#!/usr/bin/env python3
"""推送每日简报到 Discord 频道"""

import os
import requests
import json
import hashlib
from datetime import datetime
import time
import shutil
import re

from paths import BRIEFINGS_DIR, CURRENT_ENV, PUSHED_DIR, STATE_DIR, get_channel_id

DISCORD_API = "https://discord.com/api/v10"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_JSON = os.path.join(PROJECT_ROOT, "config", "sources.json")
DISCORD_CONTENT_LIMIT = 2000
DISCORD_CHUNK_LIMIT = 1950

_ARXIV_MARKER = STATE_DIR / ".arxiv_generating"
_ARXIV_POLL_INTERVAL = 30   # seconds between checks
_ARXIV_MAX_WAIT = 1800      # 30 minutes total timeout
_DISCORD_RETRY_DELAYS = (2, 5, 10)


def _wait_for_arxiv_generation(date: str) -> None:
    """If arXiv generation is in progress, poll until completion or timeout."""
    if not _ARXIV_MARKER.exists():
        return

    try:
        marker_date = _ARXIV_MARKER.read_text(encoding="utf-8").strip()
    except Exception:
        marker_date = ""

    if marker_date and marker_date != date:
        log(f"  [arxiv] stale marker for {marker_date}, ignoring (today is {date})")
        return

    log(f"  [arxiv] generation in progress, waiting (up to {_ARXIV_MAX_WAIT}s)...")
    waited = 0
    while _ARXIV_MARKER.exists() and waited < _ARXIV_MAX_WAIT:
        time.sleep(_ARXIV_POLL_INTERVAL)
        waited += _ARXIV_POLL_INTERVAL
        log(f"  [arxiv] still waiting... ({waited}s)")

    if _ARXIV_MARKER.exists():
        log(f"  [arxiv] timeout after {_ARXIV_MAX_WAIT}s, proceeding anyway")
    else:
        log(f"  [arxiv] generation finished after ~{waited}s")


def log(msg):
    """输出日志（附带当前环境标记）"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [env:{CURRENT_ENV}] {msg}", flush=True)


def _load_env_value(key):
    """Read a key from the environment or project .env, returning '' if missing."""
    val = os.environ.get(key, "")
    if val:
        return val
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return ""
    try:
        from dotenv import dotenv_values

        return dotenv_values(env_path).get(key, "") or ""
    except ImportError:
        prefix = f"{key}="
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line.startswith(prefix):
                    continue
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


DISCORD_BOT_TOKEN = _load_env_value("DISCORD_BOT_TOKEN")
if not DISCORD_BOT_TOKEN:
    log("❌ 错误：DISCORD_BOT_TOKEN 未设置")
    exit(1)

# Channel IDs are resolved per-category using the env-aware config module.
# In dev/staging environments, the keys are suffixed (e.g. DISCORD_CHANNEL_PAPERS_DEV).
# Missing entries cause that category to be skipped at push time, not a fatal error.
DISCORD_CHANNELS = {
    category: get_channel_id(category)
    for category in (
        "papers",
        "ai_news",
        "code",
        "resource",
        "arxiv",
        "conference",
        "weekly",
    )
}
# arxiv shares the ai_news Discord channel
if not DISCORD_CHANNELS.get("arxiv"):
    DISCORD_CHANNELS["arxiv"] = DISCORD_CHANNELS.get("ai_news")

log(
    f"环境: {CURRENT_ENV}  频道映射: { {k: (v or '(未配置)') for k, v in DISCORD_CHANNELS.items()} }"
)


def _today() -> str:
    """Return today's date string (YYYY-MM-DD), evaluated at call time."""
    return datetime.now().strftime("%Y-%m-%d")


# Module-level default kept for backwards compat with tooling that may read it,
# but all code paths resolve the actual date via ``_today()`` or an explicit
# ``date`` argument so callers can backfill past days.
DATE = _today()


def split_message(content, max_length=DISCORD_CHUNK_LIMIT):
    """Split long content into Discord-sized message bodies."""
    if len(content) <= max_length:
        return [content]

    messages = []
    current = ""

    for line in content.split("\n"):
        if len(line) > max_length:
            if current:
                messages.append(current)
                current = ""
            for start in range(0, len(line), max_length):
                messages.append(line[start : start + max_length])
            continue
        if len(current) + len(line) + 1 > max_length:
            if current:
                messages.append(current)
            current = line
        else:
            if current:
                current += "\n" + line
            else:
                current = line

    if current:
        messages.append(current)

    return messages


def _chunk_prefix(index, total):
    """Return the prefix added to chunked Discord messages."""
    return f"【第 {index}/{total} 部分】\n\n"


def split_discord_messages(content):
    """Split content while reserving room for chunk prefixes."""
    messages = split_message(content, DISCORD_CHUNK_LIMIT)
    if len(messages) <= 1:
        return messages

    # Re-split with the exact prefix budget once the chunk count is known.
    total = len(messages)
    prefix_budget = len(_chunk_prefix(total, total))
    max_body_length = DISCORD_CONTENT_LIMIT - prefix_budget
    messages = split_message(content, max_body_length)

    # If digit growth changed the total, split once more with the final budget.
    total = len(messages)
    prefix_budget = len(_chunk_prefix(total, total))
    max_body_length = DISCORD_CONTENT_LIMIT - prefix_budget
    return split_message(content, max_body_length)


def _post_single_message(channel_id, headers, data, chunk_index):
    """向 Discord 发送一条消息，网络失败时指数退避重试。

    Returns:
        True: 发送成功
        False: 重试耗尽或收到不可重试的 HTTP 错误
    """
    last_err = None
    for attempt, delay in enumerate(_DISCORD_RETRY_DELAYS + (None,), start=1):
        try:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                log(f"  ✅ 第 {chunk_index} 部分发送成功")
                time.sleep(0.5)
                return True
            # 429 Rate limit — honour Retry-After
            if resp.status_code == 429:
                wait = float(resp.json().get(
                    "retry_after",
                    delay if delay is not None else _DISCORD_RETRY_DELAYS[-1],
                ))
                log(f"  ⏳ 触发限速，等待 {wait:.1f}s 后重试 (第 {attempt} 次)")
                time.sleep(wait)
                last_err = "429 rate limit"
                continue
            log(f"  ❌ 第 {chunk_index} 部分发送失败: {resp.status_code} - {resp.text}")
            return False
        except Exception as e:
            last_err = str(e)
            if delay is None:
                log(f"  ❌ 发送错误（已重试 {len(_DISCORD_RETRY_DELAYS)} 次）: {last_err}")
                return False
            log(f"  ⚠️  网络错误，{delay}s 后重试 (第 {attempt} 次): {last_err}")
            time.sleep(delay)
    # Exhausted all retries (pure 429 exhaustion — unlikely but safe)
    log(f"  ❌ 第 {chunk_index} 部分重试耗尽: {last_err}")
    return False


def send_to_discord(channel_id, content, nonce_prefix=None):
    """发送消息到 Discord 频道，网络失败时最多重试 3 次（指数退避）"""
    messages = split_discord_messages(content)

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/dailyinfo, 1.0)",
    }

    for i, msg in enumerate(messages):
        if len(messages) > 1:
            msg = f"{_chunk_prefix(i + 1, len(messages))}{msg}"
        data = {"content": msg}
        if nonce_prefix:
            nonce = hashlib.sha256(
                f"{nonce_prefix}:text:{i}".encode("utf-8")
            ).hexdigest()[:25]
            data.update(nonce=nonce, enforce_nonce=True)

        if not _post_single_message(channel_id, headers, data, i + 1):
            return False

    return True


def send_figure_to_discord(
    channel_id, image_path, *, title="", caption="", nonce_prefix=None
):
    """Upload one cached architecture image as a Discord embed.

    Discord requires multipart/form-data for files; deliberately do not set a
    Content-Type header here so ``requests`` can provide the boundary.
    """

    image_path = os.fspath(image_path)
    if not os.path.isfile(image_path):
        log(f"  ❌ 架构图不存在: {image_path}")
        return False
    filename = os.path.basename(image_path)
    description = "\n".join(
        part.strip() for part in (title, caption) if str(part or "").strip()
    )[:1024]
    payload = {
        "content": "🧩 模型架构图",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "模型架构图" if not title else f"模型架构图｜{title[:200]}",
                "description": description,
                "image": {"url": f"attachment://{filename}"},
            }
        ],
        "attachments": [{"id": 0, "filename": filename, "description": description}],
    }
    if nonce_prefix:
        payload.update(
            nonce=hashlib.sha256(
                f"{nonce_prefix}:figure:{filename}".encode("utf-8")
            ).hexdigest()[:25],
            enforce_nonce=True,
        )
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "DiscordBot (https://github.com/dailyinfo, 1.0)",
    }
    last_err = None
    for attempt, delay in enumerate(_DISCORD_RETRY_DELAYS + (None,), start=1):
        try:
            with open(image_path, "rb") as handle:
                response = requests.post(
                    f"{DISCORD_API}/channels/{channel_id}/messages",
                    headers=headers,
                    data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                    files={"files[0]": (filename, handle, "image/png")},
                    timeout=30,
                )
            if response.status_code in (200, 201):
                log(f"  ✅ 架构图发送成功: {filename}")
                time.sleep(0.5)
                return True
            if response.status_code == 429:
                wait = float(
                    response.json().get(
                        "retry_after",
                        delay if delay is not None else _DISCORD_RETRY_DELAYS[-1],
                    )
                )
                log(f"  ⏳ 架构图触发限速，等待 {wait:.1f}s 后重试 (第 {attempt} 次)")
                time.sleep(wait)
                last_err = "429 rate limit"
                continue
            log(f"  ❌ 架构图发送失败: {response.status_code} - {response.text}")
            return False
        except Exception as exc:
            last_err = str(exc)
            if delay is None:
                log(f"  ❌ 架构图发送错误（已重试）: {last_err}")
                return False
            log(f"  ⚠️ 架构图网络错误，{delay}s 后重试 (第 {attempt} 次): {last_err}")
            time.sleep(delay)
    return False


def is_placeholder(content):
    """Return True when content is a short no-update placeholder."""
    # Placeholders only contain the no-update notice generated by run_pipelines.
    return "📭 过去" in content and "无新内容" in content and len(content.strip()) < 200


def is_low_quality_content(content):
    """Return True for extremely short non-Chinese content."""
    stripped = content.strip()

    if len(stripped) < 100 and not any("\u4e00" <= c <= "\u9fff" for c in stripped):
        return True

    return False


def _load_sources_by_category(category):
    """Load enabled sources for a category from config/sources.json."""
    try:
        with open(SOURCES_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"  ⚠️  读取 sources.json 失败，无法生成来源总结: {e}")
        return []
    return [
        source
        for source in cfg.get("sources", [])
        if source.get("category") == category and source.get("enabled", True)
    ]


def _source_name_from_filename(filename, sources):
    """Resolve a briefing filename back to a configured source name."""
    for source in sorted(
        sources, key=lambda src: len(src.get("name", "")), reverse=True
    ):
        name = source.get("name", "")
        if filename.startswith(f"{name}_briefing_"):
            return name
    return filename.split("_briefing_", 1)[0]


def _format_source_list(names, display_names):
    """Format source names with configured display names for Discord."""
    if not names:
        return "无"
    return "\n".join(f"- {display_names.get(name, name)} (`{name}`)" for name in names)


def build_push_summary(
    category, date, pushed_names, placeholder_names, pending_names=None
):
    """Build a deterministic per-category push summary message."""
    sources = _load_sources_by_category(category)
    if not sources:
        return ""

    configured_names = [source["name"] for source in sources]
    display_names = {
        source["name"]: source.get("display_name", source["name"]) for source in sources
    }
    pushed_set = set(pushed_names)
    placeholder_set = set(placeholder_names)
    pending_set = set(pending_names or [])

    no_update_names = [
        name
        for name in configured_names
        if name in placeholder_set
        and name not in pushed_set
        and name not in pending_set
    ]
    missing_names = [
        name
        for name in configured_names
        if name not in pushed_set
        and name not in placeholder_set
        and name not in pending_set
    ]

    title = (
        "📊 论文频道推送总结"
        if category in ("papers", "arxiv")
        else f"📊 {category} 推送总结"
    )
    lines = [
        f"{title} ({date})",
        "",
        f"✅ 已推送期刊 ({len(pushed_set)}):",
        _format_source_list(
            [n for n in configured_names if n in pushed_set], display_names
        ),
        "",
        f"📭 今日无文章更新 ({len(no_update_names)}):",
        _format_source_list(no_update_names, display_names),
    ]
    if missing_names:
        lines.extend(
            [
                "",
                f"⚠️ 未发现今日简报文件 ({len(missing_names)}):",
                _format_source_list(missing_names, display_names),
            ]
        )
    return "\n".join(lines)


def _cleanup_placeholder_files(filepaths):
    """Remove placeholder files after their no-update status has been reported."""
    for filepath in filepaths:
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            if is_placeholder(content):
                os.remove(filepath)
        except OSError as e:
            log(f"  ⚠️  清理 {os.path.basename(filepath)} 出错: {e}")


def _receipt_path(filename):
    receipt_dir = STATE_DIR / "discord_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    return receipt_dir / f"{filename}.json"


def _load_receipt(filename):
    path = _receipt_path(filename)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_receipt(filename, receipt):
    path = _receipt_path(filename)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_figure_sidecar(filepath):
    sidecar = os.path.splitext(filepath)[0] + ".assets.json"
    try:
        with open(sidecar, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}, sidecar
    except (OSError, ValueError):
        return {}, sidecar


def _conference_sections(content):
    """Split a conference briefing into the intro and ``###`` paper sections."""

    matches = list(re.finditer(r"(?m)^###\s+(.+?)\s*$", content))
    if not matches:
        return [("briefing", "", content)]
    sections = []
    if matches[0].start():
        sections.append(("intro", "", content[: matches[0].start()].strip()))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections.append(
            (f"paper:{index}", match.group(1).strip(), content[match.start() : end].strip())
        )
    return [(key, title, text) for key, title, text in sections if text]


def _title_key(value):
    """Normalize a paper title for matching sidecar metadata to Markdown."""

    value = re.sub(r"\([^)]*状态[^)]*\)", "", str(value or ""), flags=re.I)
    value = re.sub(r"（[^）]*状态[^）]*）", "", value)
    return re.sub(r"[^\w\u3400-\u9fff]", "", value.casefold())


def _match_conference_attachments(sections, attachments):
    """Map each figure attachment to the paper section containing its title."""

    matched = {}
    unused = []
    for attachment in attachments:
        title = str(attachment.get("title") or "")
        forum_id = str(attachment.get("forum_id") or "")
        title_key = _title_key(title)
        found = None
        for index, (_key, section_title, section_text) in enumerate(sections):
            section_key = _title_key(section_title)
            if title_key and section_key and (
                title_key in section_key or section_key in title_key
            ):
                found = index
                break
            if forum_id and forum_id in section_text:
                found = index
                break
        if found is None:
            unused.append(attachment)
        else:
            matched.setdefault(found, []).append(attachment)
    return matched, unused


def send_conference_briefing(
    channel_id,
    content,
    sidecar,
    receipt,
    *,
    filename="",
    nonce_prefix="conference",
):
    """Send each paper section followed immediately by its figure attachment."""

    sections = _conference_sections(content)
    attachments = [
        item for item in (sidecar or {}).get("attachments", [])
        if isinstance(item, dict)
    ]
    matched, unmatched = _match_conference_attachments(sections, attachments)
    sent_segments = {str(value) for value in receipt.get("text_segments_sent", [])}
    sent_figures = {str(value) for value in receipt.get("figures_sent", [])}
    legacy_text_sent = bool(receipt.get("text_sent"))

    def save():
        if filename:
            _save_receipt(filename, receipt)

    for section_index, (segment_key, _title, text) in enumerate(sections):
        if not legacy_text_sent and segment_key not in sent_segments:
            if not send_to_discord(
                channel_id,
                text,
                nonce_prefix=f"{nonce_prefix}:segment:{segment_key}",
            ):
                return False
            sent_segments.add(segment_key)
            receipt["text_segments_sent"] = sorted(sent_segments)
            save()

        for attachment in matched.get(section_index, []):
            manifest = attachment.get("manifest") or {}
            image_path = manifest.get("path")
            if not image_path:
                continue
            figure_key = str(attachment.get("event_id") or image_path)
            if figure_key in sent_figures:
                continue
            if not send_figure_to_discord(
                channel_id,
                image_path,
                title=attachment.get("title", ""),
                caption=manifest.get("caption", ""),
                nonce_prefix=f"{nonce_prefix}:{figure_key}",
            ):
                return False
            sent_figures.add(figure_key)
            receipt["figures_sent"] = sorted(sent_figures)
            save()

    # Preserve useful images even when a malformed/legacy briefing title did
    # not match a section; they are sent after the text as a safe fallback.
    for attachment in unmatched:
        manifest = attachment.get("manifest") or {}
        image_path = manifest.get("path")
        if not image_path:
            continue
        figure_key = str(attachment.get("event_id") or image_path)
        if figure_key in sent_figures:
            continue
        if not send_figure_to_discord(
            channel_id,
            image_path,
            title=attachment.get("title", ""),
            caption=manifest.get("caption", ""),
            nonce_prefix=f"{nonce_prefix}:{figure_key}:unmatched",
        ):
            return False
        sent_figures.add(figure_key)
        receipt["figures_sent"] = sorted(sent_figures)
        save()

    receipt["text_segments_sent"] = sorted(sent_segments)
    receipt["figures_sent"] = sorted(sent_figures)
    receipt["text_sent"] = True
    save()
    return True


def push_category(category, channel_id, date=None):
    """Push every briefing for ``category`` whose filename contains ``date``.

    Args:
        category: Briefing category name (e.g. "papers").
        channel_id: Target Discord channel id.
        date: Date string (YYYY-MM-DD). Defaults to today when omitted so
            existing callers keep working; pass an older date to backfill.
    """
    date = date or _today()
    category_dir = os.path.join(BRIEFINGS_DIR, category)

    if not os.path.exists(category_dir):
        log(f"  ⚠️  {category} 目录不存在")
        return 0

    if category == "arxiv":
        _wait_for_arxiv_generation(date)

    files = [
        f for f in sorted(os.listdir(category_dir))
        if date in f and f.endswith(".md")
    ]

    if not files:
        log(f"  ℹ️  {category} 中没有 {date} 的文件，发送无内容提醒")
        notice = f"📭 **{category}** 频道：{date} 暂无新简报"
        send_to_discord(channel_id, notice)
        return 0

    log(f"  发现 {len(files)} 份文件...")

    # Keep real briefing files separate from placeholders used for status.
    valid_files = []
    sources = _load_sources_by_category(category)
    placeholder_names = []
    placeholder_paths = []
    pending_names = []
    pushed_names = []
    placeholder_count = 0
    low_quality_count = 0

    for filename in files:
        filepath = os.path.join(category_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if is_placeholder(content):
                placeholder_count += 1
                placeholder_names.append(_source_name_from_filename(filename, sources))
                placeholder_paths.append(filepath)
                log(f"    ⊘ {filename} (无内容，待汇总后清理)")
            elif is_low_quality_content(content):
                low_quality_count += 1
                # Drop low-quality files because they cannot produce useful status.
                os.remove(filepath)
                log(f"    ⊘ {filename} (低质量内容，已删除)")
            else:
                valid_files.append((filename, filepath, content))
        except Exception as e:
            log(f"  ❌ 读取 {filename} 出错: {e}")

    if valid_files:
        log(
            f"  有效文件: {len(valid_files)} 份，空内容: {placeholder_count} 份，低质量: {low_quality_count} 份"
        )
        log("  开始推送...")
    else:
        total_filtered = placeholder_count + low_quality_count
        log(
            f"  全部被过滤 (空内容: {placeholder_count}, 低质量: {low_quality_count}, 共 {total_filtered} 份)，发送无内容提醒"
        )
        summary = (
            build_push_summary(category, date, [], placeholder_names)
            if category in ("papers", "arxiv")
            else ""
        )
        if summary and send_to_discord(channel_id, summary):
            _cleanup_placeholder_files(placeholder_paths)
        else:
            notice = f"📭 **{category}** 频道：{date} 各源均无新内容"
            if send_to_discord(channel_id, notice):
                _cleanup_placeholder_files(placeholder_paths)
        return 0

    pushed_count = 0
    for filename, filepath, content in valid_files:
        try:
            # Send the real briefing before archiving it.
            receipt = _load_receipt(filename)
            nonce_prefix = f"{category}:{filename}"
            text_sent = bool(receipt.get("text_sent"))
            if category == "conference":
                sidecar, _sidecar_path = _load_figure_sidecar(filepath)
                text_result = send_conference_briefing(
                    channel_id,
                    content,
                    sidecar,
                    receipt,
                    filename=filename,
                    nonce_prefix=nonce_prefix,
                )
                text_sent = bool(receipt.get("text_sent")) and text_result
            elif text_sent:
                text_result = True
            else:
                # Keep the legacy two-argument call for non-conference
                # categories and their existing integrations/tests.
                text_result = send_to_discord(channel_id, content)
            if not text_sent and text_result:
                receipt["text_sent"] = True
                _save_receipt(filename, receipt)
                text_sent = True
            if category == "conference" and not text_sent:
                pending_names.append(_source_name_from_filename(filename, sources))
                log(f"    ✗ {filename} 会议简报/架构图推送失败，保留原位")
                continue
            if text_sent:
                # Move only successfully sent files to the pushed archive.
                pushed_category_dir = os.path.join(PUSHED_DIR, category)
                os.makedirs(pushed_category_dir, exist_ok=True)

                dest_path = os.path.join(pushed_category_dir, filename)
                shutil.move(filepath, dest_path)
                sidecar_path = os.path.splitext(filepath)[0] + ".assets.json"
                if os.path.exists(sidecar_path):
                    shutil.move(
                        sidecar_path,
                        os.path.join(pushed_category_dir, os.path.basename(sidecar_path)),
                    )

                log(f"    ✓ {filename} 推送完成")
                pushed_count += 1
                pushed_names.append(_source_name_from_filename(filename, sources))
                time.sleep(1)  # Avoid sending files back-to-back too quickly.
            else:
                log(f"    ✗ {filename} 推送失败，保留原位")
                pending_names.append(_source_name_from_filename(filename, sources))

        except Exception as e:
            log(f"  ❌ 处理 {filename} 出错: {e}")

    if category in ("papers", "arxiv"):
        summary = build_push_summary(
            category, date, pushed_names, placeholder_names, pending_names
        )
        if summary and send_to_discord(channel_id, summary):
            _cleanup_placeholder_files(placeholder_paths)
    else:
        _cleanup_placeholder_files(placeholder_paths)

    return pushed_count


def _parse_date(value):
    """Validate and normalise a YYYY-MM-DD date string."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid --date value {value!r}; expected YYYY-MM-DD"
        ) from exc


ALL_CATEGORIES = [
    "papers",
    "ai_news",
    "code",
    "resource",
    "arxiv",
    "conference",
    "weekly",
]
DAILY_CATEGORIES = ["papers", "ai_news", "code", "resource", "arxiv", "conference"]


def main(date=None, categories=None):
    date = date or _today()
    active = categories if categories is not None else DAILY_CATEGORIES

    log("=== Discord 推送开始 ===")
    log(f"日期: {date}")
    log(f"频道: {', '.join(active)}")

    total_pushed = 0

    PUSH_ORDER = [
        "papers",
        "conference",
        "code",
        "resource",
        "ai_news",
        "arxiv",
        "weekly",
    ]
    for category in PUSH_ORDER:
        if category not in active:
            continue
        channel_id = DISCORD_CHANNELS.get(category, "")
        if not channel_id:
            log(f"⚠️  {category} 未配置 DISCORD_CHANNEL_{category.upper()}，跳过")
            continue
        log(f"推送到 #{category}...")
        count = push_category(category, channel_id, date)
        total_pushed += count
        log(f"  小计: {count} 份文件")

    log("=== 推送完成 ===")
    log(f"总共推送: {total_pushed} 份文件")

    return 0 if total_pushed > 0 else 1


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Push daily briefings to Discord.")
    parser.add_argument(
        "--date",
        default=None,
        help="Date to push in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help=(
            "Comma-separated list of categories to push "
            "(e.g. 'papers,ai_news,code,resource' or 'weekly'). "
            f"Defaults to all: {','.join(ALL_CATEGORIES)}."
        ),
    )
    args = parser.parse_args()

    resolved_date = _parse_date(args.date) if args.date else None
    resolved_cats = (
        [c.strip() for c in args.categories.split(",") if c.strip()]
        if args.categories
        else None
    )
    sys.exit(main(resolved_date, resolved_cats))
