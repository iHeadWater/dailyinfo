#!/usr/bin/env python3
"""推送每日简报到 Discord 频道"""

import os
import requests
import json
from datetime import datetime
import time
import shutil

from paths import BRIEFINGS_DIR, PUSHED_DIR

DISCORD_API = "https://discord.com/api/v10"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_JSON = os.path.join(PROJECT_ROOT, "config", "sources.json")
DISCORD_CONTENT_LIMIT = 2000
DISCORD_CHUNK_LIMIT = 1950


def log(msg):
    """输出日志"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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
        with open(env_path) as f:
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

# Channel IDs are loaded per-category from env (DISCORD_CHANNEL_<CATEGORY>).
# Missing entries cause that category to be skipped at push time, not a fatal error.
DISCORD_CHANNELS = {
    category: _load_env_value(f"DISCORD_CHANNEL_{category.upper()}")
    for category in ("papers", "ai_news", "code", "resource")
}


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


def send_to_discord(channel_id, content):
    """发送消息到 Discord 频道"""
    messages = split_discord_messages(content)

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/dailyinfo, 1.0)",
    }

    for i, msg in enumerate(messages):
        try:
            # Add a human-readable chunk marker when a briefing spans messages.
            if len(messages) > 1:
                msg = f"{_chunk_prefix(i + 1, len(messages))}{msg}"

            data = {"content": msg}

            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=data,
                timeout=10,
            )

            if resp.status_code in (200, 201):
                log(f"  ✅ 第 {i+1} 部分发送成功")
                time.sleep(0.5)
            else:
                log(f"  ❌ 第 {i+1} 部分发送失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            log(f"  ❌ 发送错误: {e}")
            return False

    return True


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

    title = "📊 论文频道推送总结" if category == "papers" else f"📊 {category} 推送总结"
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

    files = [f for f in sorted(os.listdir(category_dir)) if date in f]

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
        log(f"  开始推送...")
    else:
        total_filtered = placeholder_count + low_quality_count
        log(
            f"  全部被过滤 (空内容: {placeholder_count}, 低质量: {low_quality_count}, 共 {total_filtered} 份)，发送无内容提醒"
        )
        summary = (
            build_push_summary(category, date, [], placeholder_names)
            if category == "papers"
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
            if send_to_discord(channel_id, content):
                # Move only successfully sent files to the pushed archive.
                pushed_category_dir = os.path.join(PUSHED_DIR, category)
                os.makedirs(pushed_category_dir, exist_ok=True)

                dest_path = os.path.join(pushed_category_dir, filename)
                shutil.move(filepath, dest_path)

                log(f"    ✓ {filename} 推送完成")
                pushed_count += 1
                pushed_names.append(_source_name_from_filename(filename, sources))
                time.sleep(1)  # Avoid sending files back-to-back too quickly.
            else:
                log(f"    ✗ {filename} 推送失败，保留原位")
                pending_names.append(_source_name_from_filename(filename, sources))

        except Exception as e:
            log(f"  ❌ 处理 {filename} 出错: {e}")

    if category == "papers":
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


def main(date=None):
    date = date or _today()

    log("=== Discord 推送开始 ===")
    log(f"日期: {date}")

    total_pushed = 0

    for category in ["papers", "ai_news", "code", "resource"]:
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
    args = parser.parse_args()

    resolved = _parse_date(args.date) if args.date else None
    sys.exit(main(resolved))
