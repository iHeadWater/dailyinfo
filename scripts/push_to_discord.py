#!/usr/bin/env python3
"""推送每日简报到 Discord 频道"""

import os
import requests
import json
from datetime import datetime
import time
import shutil

# Discord API 端点
DISCORD_API = "https://discord.com/api/v10"

# 项目根目录（脚本位于 scripts/，上一级即根目录）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def log(msg):
    """输出日志"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def _load_discord_token():
    """从 .env 或环境变量读取 DISCORD_BOT_TOKEN。"""
    # 优先环境变量
    token = os.environ.get('DISCORD_BOT_TOKEN', '')
    if token:
        return token
    # 从 .env 文件解析
    env_path = os.path.join(PROJECT_ROOT, '.env')
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values
            token = dotenv_values(env_path).get('DISCORD_BOT_TOKEN', '')
        except ImportError:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DISCORD_BOT_TOKEN=') and not line.startswith('#'):
                        token = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
    return token

# Discord 配置
DISCORD_BOT_TOKEN = _load_discord_token()
if not DISCORD_BOT_TOKEN:
    log("❌ 错误：DISCORD_BOT_TOKEN 未设置")
    exit(1)

DISCORD_CHANNELS = {
    "papers": "1489102139597787181",
    "ai_news": "1489102139597787182",
    "code": "1489102139597787183",
    "resource": "1489102139597787178"
}

BRIEFINGS_DIR = os.path.expanduser("~/.openclaw/workspace/briefings")
PUSHED_DIR = os.path.expanduser("~/.openclaw/workspace/pushed")
DATE = datetime.now().strftime("%Y-%m-%d")

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
        "Content-Type": "application/json"
    }

    for i, msg in enumerate(messages):
        try:
            # 如果是分段消息，添加分段标记
            if len(messages) > 1:
                msg = f"【第 {i+1}/{len(messages)} 部分】\n\n{msg}"

            data = {
                "content": msg
            }

            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=data,
                timeout=10
            )

            if resp.status_code == 200:
                log(f"  ✅ 第 {i+1} 部分发送成功")
                time.sleep(0.5)  # 避免速率限制
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
    """检查是否是低质量内容（只有目录/简介，没有实际文章）"""
    lines = content.strip().split('\n')

    # 去掉标题行（以#开头）和空行
    meaningful_lines = [l for l in lines if l.strip() and not l.startswith('#') and not l.startswith('*')]

    # 如果内容只包含"In This Issue"且很短，视为低质量
    if "In This Issue" in content and len(content) < 500:
        return True

    # 关键：检查是否包含实际的摘要信息（用"—"或"："分隔的描述）
    has_descriptions = False
    for line in meaningful_lines:
        # 检查是否有"[日期] 标题 — 描述"这样的格式
        if " — " in line or " : " in line or " ：" in line:
            # 检查"—"后面是否有实际内容（不只是链接）
            parts = line.split(" — " if " — " in line else (" ：" if " ：" in line else " : "))
            if len(parts) > 1 and len(parts[1].strip()) > 10:  # 描述至少 10 个字
                has_descriptions = True
                break

    # 如果没有任何实际描述，视为低质量
    if not has_descriptions and len(content) < 200:
        return True

    return False

def push_category(category, channel_id):
    """推送某个分类的所有今日文件"""
    category_dir = os.path.join(BRIEFINGS_DIR, category)

    if not os.path.exists(category_dir):
        log(f"  ⚠️  {category} 目录不存在")
        return 0

    # 找到今天的文件
    files = [f for f in sorted(os.listdir(category_dir)) if DATE in f]

    if not files:
        log(f"  ℹ️  {category} 中没有今天的文件，发送无内容提醒")
        notice = f"📭 **{category}** 频道：{DATE} 暂无新简报"
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
            with open(filepath, 'r', encoding='utf-8') as f:
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
        log(f"  有效文件: {len(valid_files)} 份，空内容: {placeholder_count} 份，低质量: {low_quality_count} 份")
        log(f"  开始推送...")
    else:
        total_filtered = placeholder_count + low_quality_count
        log(f"  全部被过滤 (空内容: {placeholder_count}, 低质量: {low_quality_count}, 共 {total_filtered} 份)，发送无内容提醒")
        notice = f"📭 **{category}** 频道：{DATE} 各源均无新内容"
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

def main():
    log("=== Discord 推送开始 ===")
    log(f"日期: {DATE}")

    total_pushed = 0

    # 推送顺序：papers -> ai_news -> code -> resource
    for category in ["papers", "ai_news", "code", "resource"]:
        if category in DISCORD_CHANNELS:
            channel_id = DISCORD_CHANNELS[category]
            log(f"推送到 #{category}...")
            count = push_category(category, channel_id)
            total_pushed += count
            log(f"  小计: {count} 份文件")

    log("=== 推送完成 ===")
    log(f"总共推送: {total_pushed} 份文件")

    return 0 if total_pushed > 0 else 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
