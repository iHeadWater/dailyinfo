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


def split_message(content, max_length=1950):
    """分割超长消息（Discord 单条消息上限 2000 字符）"""
    if len(content) <= max_length:
        return [content]

    messages = []
    current = ""

    for line in content.split("\n"):
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


def send_to_discord(channel_id, content):
    """发送消息到 Discord 频道"""
    messages = split_message(content)

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/dailyinfo, 1.0)",
    }

    for i, msg in enumerate(messages):
        try:
            # 如果是分段消息，添加分段标记
            if len(messages) > 1:
                msg = f"【第 {i+1}/{len(messages)} 部分】\n\n{msg}"

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
    """检查是否是空内容的 placeholder 文件"""
    # 如果内容只包含 "📭 过去 X 小时无新内容"，则是 placeholder
    return "📭 过去" in content and "无新内容" in content and len(content.strip()) < 200


def is_low_quality_content(content):
    """检查是否是低质量内容：仅过滤真正无意义的极短内容"""
    stripped = content.strip()

    if len(stripped) < 100 and not any("\u4e00" <= c <= "\u9fff" for c in stripped):
        return True

    return False


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

    # 先过滤出有实际内容的文件（不是 placeholder）
    valid_files = []
    placeholder_count = 0
    low_quality_count = 0

    for filename in files:
        filepath = os.path.join(category_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if is_placeholder(content):
                placeholder_count += 1
                # 删除 placeholder 文件（不推送，直接删除）
                os.remove(filepath)
                log(f"    ⊘ {filename} (无内容，已删除)")
            elif is_low_quality_content(content):
                low_quality_count += 1
                # 删除低质量文件（只有目录，没有实际内容）
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
        notice = f"📭 **{category}** 频道：{date} 各源均无新内容"
        send_to_discord(channel_id, notice)
        return 0

    pushed_count = 0
    for filename, filepath, content in valid_files:
        try:
            # 发送到 Discord
            if send_to_discord(channel_id, content):
                # 移到 pushed 目录
                pushed_category_dir = os.path.join(PUSHED_DIR, category)
                os.makedirs(pushed_category_dir, exist_ok=True)

                dest_path = os.path.join(pushed_category_dir, filename)
                shutil.move(filepath, dest_path)

                log(f"    ✓ {filename} 推送完成")
                pushed_count += 1
                time.sleep(1)  # 频道之间的延迟
            else:
                log(f"    ✗ {filename} 推送失败，保留原位")

        except Exception as e:
            log(f"  ❌ 处理 {filename} 出错: {e}")

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
