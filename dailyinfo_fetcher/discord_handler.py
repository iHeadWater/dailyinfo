"""
合并版 Discord Bot：
  - @bot {query}（不回复消息）→ 搜索本地 briefings + AI 分析（原 discord_bot.py 功能）
  - 回复消息 + @bot {query}   → 深度获取：新闻/GitHub 卡片
  - 📥 reaction on 论文简报   → 下载论文 PDF
"""
import asyncio
import glob
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import discord
import httpx

from .config import DISCORD_BOT_TOKEN, DOWNLOAD_DIR, OPENROUTER_API_KEY, GITHUB_TOKEN, GITHUB_REPO
from .message_parser import (
    extract_paper_titles,
    is_paper_message,
    is_github_related,
    resolve_intent_local,
    resolve_intent_claude,
    extract_items,
)
from .paper_fetcher import fetch_all_papers
from .news_fetcher import fetch_news_deep
from .github_fetcher import extract_github_repo, fetch_github_card
from .utils import get_logger, split_message

log = get_logger("discord_handler")

# ---------------------------------------------------------------------------
# Paths (same as original discord_bot.py)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent
try:
    from scripts.paths import BRIEFINGS_DIR, PUSHED_DIR
except Exception:
    BRIEFINGS_DIR = Path.home() / ".myagentdata" / "dailyinfo" / "briefings"
    PUSHED_DIR    = Path.home() / ".myagentdata" / "dailyinfo" / "pushed"

LOOKBACK_DAYS = 3
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANALYSIS_MODEL = "anthropic/claude-sonnet-4-5"

# ---------------------------------------------------------------------------
# Briefing search (ported from original discord_bot.py)
# ---------------------------------------------------------------------------

def _recent_briefing_files() -> list[str]:
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    files = []
    for root_dir in (PUSHED_DIR, BRIEFINGS_DIR):
        for path in glob.glob(os.path.join(root_dir, "**", "*.md"), recursive=True):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
            if m:
                try:
                    if datetime.strptime(m.group(1), "%Y-%m-%d") >= cutoff:
                        files.append(path)
                except ValueError:
                    pass
    return files


def _search_briefings(query: str) -> list[tuple[str, str]]:
    keywords = [w.lower() for w in re.split(r"\s+", query.strip()) if len(w) > 1]
    results = []
    for path in _recent_briefing_files():
        try:
            content = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if "📭 过去" in content and len(content.strip()) < 200:
            continue
        score = sum(1 for kw in keywords if kw in content.lower())
        if score > 0:
            results.append((score, path, content))
    results.sort(key=lambda x: x[0], reverse=True)
    return [(os.path.basename(p), c) for _, p, c in results[:3]]


def _build_context(query: str) -> str:
    matches = _search_briefings(query)
    if not matches:
        return ""
    parts = []
    for fname, content in matches:
        trimmed = content[:3000] + ("\n...[内容过长已截断]" if len(content) > 3000 else "")
        parts.append(f"=== 来源: {fname} ===\n{trimmed}")
    return "\n\n".join(parts)


async def _call_ai_analysis(user_query: str, context: str) -> str:
    if context:
        user_msg = f"用户问题：{user_query}\n\n以下是相关的每日简报内容，请基于此作答：\n\n{context}"
    else:
        user_msg = (
            f"用户问题：{user_query}\n\n"
            "（未在近期简报中找到相关内容，请凭知识作答，并在回复末尾注明"
            "「注：未找到相关简报，以上为通用分析」。）"
        )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": ANALYSIS_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "你是 DailyInfo 的科研情报分析助手。基于提供的简报内容进行深度解析，"
                            "包括：核心贡献、方法原理、与领域现状的关系、潜在影响和局限性。"
                            "回答要专业、有深度，使用中文，适当使用 Markdown 格式。"
                        )},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 2000,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"AI analysis failed: {e}")
        return f"❌ AI 分析出错：{e}"

# ---------------------------------------------------------------------------
# GitHub issue creation
# ---------------------------------------------------------------------------

async def _classify_issue_intent(query: str) -> tuple[bool, str, str]:
    """Use AI to detect if message is a bug report / feature request.
    Returns (is_issue, title, body). title/body are empty if not an issue.
    """
    prompt = (
        f"用户发送了这条消息：「{query}」\n\n"
        "判断：这是在（A）反馈bug、提出功能需求、报告问题、希望改进某功能，"
        "还是（B）提问、查询信息、寻求解释？\n\n"
        "如果是 A，严格按以下格式输出，不要其他内容：\n"
        "INTENT: issue\n"
        "TITLE: <简洁标题，15字以内>\n"
        "BODY: <详细描述，100字以内>\n\n"
        "如果是 B，输出：\n"
        "INTENT: query"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": ANALYSIS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        if "INTENT: issue" not in text:
            return False, "", ""
        title, body = "", query
        for line in text.splitlines():
            if line.startswith("TITLE:"):
                title = line.removeprefix("TITLE:").strip()
            elif line.startswith("BODY:"):
                body = line.removeprefix("BODY:").strip()
        return True, title or query[:50], body
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return False, "", ""


async def _generate_issue_fields(raw: str) -> tuple[str, str]:
    """Use AI to turn a casual sentence into a (title, body) pair."""
    prompt = (
        f"用户用一句话描述了一个问题或需求：\n\n「{raw}」\n\n"
        "请输出一个 GitHub Issue 的标题和正文（中文）。\n"
        "格式严格如下，不要输出其他内容：\n"
        "TITLE: <简洁标题，15字以内>\n"
        "BODY: <详细描述，100字以内>"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": ANALYSIS_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
            },
        )
        resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    title, body = "", raw
    for line in text.splitlines():
        if line.startswith("TITLE:"):
            title = line.removeprefix("TITLE:").strip()
        elif line.startswith("BODY:"):
            body = line.removeprefix("BODY:").strip()
    return title or raw[:50], body


async def _create_github_issue(title: str, body: str) -> str:
    """POST to GitHub API and return the new issue URL."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"title": title, "body": body},
        )
        resp.raise_for_status()
    return resp.json()["html_url"]


async def _handle_create_issue(message: discord.Message, raw_text: str) -> None:
    """Generate and submit a GitHub issue from a Discord message."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await message.reply("❌ 未配置 GITHUB_TOKEN / GITHUB_REPO，无法提交 issue。")
        return

    author = str(message.author)
    channel = message.channel.name if hasattr(message.channel, "name") else str(message.channel.id)

    await message.channel.typing()
    try:
        title, body = await _generate_issue_fields(raw_text)
        full_body = f"{body}\n\n---\n_由 Discord 用户 @{author} 在 #{channel} 频道提交_"
        url = await _create_github_issue(title, full_body)
        await message.reply(f"✅ Issue 已创建：**{title}**\n{url}")
        log.info(f"Created issue: {url}")
    except Exception as e:
        log.error(f"Failed to create issue: {e}")
        await message.reply(f"❌ 创建 issue 失败：{e}")


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

_PROXY = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or None
bot = discord.Client(intents=intents, proxy=_PROXY)


async def _send_long(channel, text: str, reference=None) -> None:
    for i, part in enumerate(split_message(text)):
        if i == 0 and reference:
            await reference.reply(part)
        else:
            await channel.send(part)
        if i > 0:
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Handler: paper download (📥 reaction)
# ---------------------------------------------------------------------------

async def _handle_paper_download(message: discord.Message, channel) -> None:
    titles = extract_paper_titles(message.content)
    if not titles:
        await channel.send("⚠️ 未能从消息中解析出论文标题。")
        return

    status = await channel.send(f"⏳ 正在获取 {len(titles)} 篇论文，请稍候…")
    results = await fetch_all_papers(titles)

    lines, pdf_files = [], []
    for title, pdf_path, source in results:
        short = title[:60] + ("…" if len(title) > 60 else "")
        if pdf_path:
            lines.append(f"✅ {short} — {source}")
            pdf_files.append(pdf_path)
        else:
            lines.append(f"❌ {short} — 未能获取")

    await status.edit(content="\n".join(lines))

    batch: list[discord.File] = []
    for path in pdf_files:
        if path.stat().st_size <= 25 * 1024 * 1024:
            batch.append(discord.File(path))
        if len(batch) == 10:
            await channel.send(files=batch)
            batch = []
            await asyncio.sleep(1)
    if batch:
        await channel.send(files=batch)


# ---------------------------------------------------------------------------
# Shared helper: download one paper and report result
# ---------------------------------------------------------------------------

async def _do_single_paper_download(trigger_msg: discord.Message, title: str) -> None:
    from .paper_fetcher import fetch_paper_oa
    short = title[:60] + ("…" if len(title) > 60 else "")
    status = await trigger_msg.reply(f"⏳ 正在下载：{short}…")

    pdf_path, source = await fetch_paper_oa(title)

    if pdf_path and pdf_path.exists() and pdf_path.stat().st_size > 1000:
        await status.edit(content=f"✅ {short} — {source}")
        await trigger_msg.channel.send(file=discord.File(pdf_path))
    else:
        await status.edit(content=f"❌ {short} — 未能获取（arXiv/Unpaywall/Semantic Scholar/Crossref/PMC/图书馆均无结果）")


def _extract_title_from_query(query: str) -> str:
    """Strip download verb and surrounding noise to get a bare paper title."""
    title = re.sub(r"^.*?(?:下载|download)\s*", "", query, flags=re.IGNORECASE).strip()
    # Strip leading "这篇/这个/这" + "文章/论文/paper/article" noise
    title = re.sub(r"^(?:这篇|这个|这)\s*(?:论文|文章|paper|article)?\s*", "", title, flags=re.IGNORECASE).strip()
    # Strip trailing same noise
    title = re.sub(r"\s*(?:这篇|这个|的)?\s*(?:论文|文章|paper|article)\s*$", "", title, flags=re.IGNORECASE).strip()
    return title


# ---------------------------------------------------------------------------
# Handler: deep fetch (reply + @mention)
# ---------------------------------------------------------------------------

async def _handle_deep_fetch(original: discord.Message, reply_msg: discord.Message) -> None:
    query = re.sub(r"<@!?\d+>", "", reply_msg.content).strip()
    if not query:
        await reply_msg.reply("请告诉我你想了解哪一条内容 🔍")
        return

    await reply_msg.channel.typing()
    content = original.content

    # Paper briefing + download intent → download a specific paper
    # Match either the 🗂emoji format or any message that has bold paper titles
    _has_download = re.search(r"下载|download", query, re.IGNORECASE)
    _titles_in_msg = extract_paper_titles(content)
    if _has_download and (is_paper_message(content) or _titles_in_msg):
        items = extract_items(content)
        titles = _titles_in_msg

        # "下载第X条" → positional match
        local = resolve_intent_local(query, items)
        if local and local.index <= len(titles):
            target_title = titles[local.index - 1]
        else:
            # Direct title in query or fuzzy Claude match
            direct = _extract_title_from_query(query)
            if direct and len(direct) > 8 and not re.search(r"第\s*\d+\s*条", direct):
                target_title = direct
            elif titles:
                item_title, _ = await resolve_intent_claude(content, query)
                item_lower = item_title.lower()
                target_title = next(
                    (t for t in titles if item_lower in t.lower() or t.lower() in item_lower),
                    titles[0],
                )
            else:
                await reply_msg.reply("⚠️ 未能从消息中解析出论文标题。")
                return

        await _do_single_paper_download(reply_msg, target_title)
        return

    # GitHub card path
    if is_github_related(content) or is_github_related(query):
        repo = extract_github_repo(query) or extract_github_repo(content)
        if repo:
            log.info(f"GitHub card: {repo}")
            card = await fetch_github_card(repo)
            await _send_long(reply_msg.channel, card, reference=reply_msg)
            return

    # Resolve which item the user wants
    items = extract_items(content)
    local = resolve_intent_local(query, items)
    if local:
        item_title = local.title or local.body[:100]
        search_query = item_title
    else:
        item_title, search_query = await resolve_intent_claude(content, query)

    log.info(f"Deep fetch: {item_title[:60]}")
    result = await fetch_news_deep(item_title, search_query)
    await _send_long(reply_msg.channel, result, reference=reply_msg)


# ---------------------------------------------------------------------------
# Handler: briefing search + AI analysis (@mention without reply)
# ---------------------------------------------------------------------------

async def _handle_briefing_query(message: discord.Message) -> None:
    query = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not query:
        await message.reply("请告诉我你想深度解析哪篇论文或哪个内容 🔍")
        return

    # Issue creation intent — AI-based detection
    is_issue, title, body = await _classify_issue_intent(query)
    if is_issue:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            await message.reply("❌ 未配置 GITHUB_TOKEN / GITHUB_REPO，无法提交 issue。")
            return
        author = str(message.author)
        channel = message.channel.name if hasattr(message.channel, "name") else str(message.channel.id)
        await message.channel.typing()
        try:
            full_body = f"{body}\n\n---\n_由 Discord 用户 @{author} 在 #{channel} 频道提交_"
            url = await _create_github_issue(title, full_body)
            await message.reply(f"✅ Issue 已创建：**{title}**\n{url}")
            log.info(f"Created issue: {url}")
        except Exception as e:
            log.error(f"Failed to create issue: {e}")
            await message.reply(f"❌ 创建 issue 失败：{e}")
        return

    # Download intent without an explicit reply
    if re.search(r"下载|download", query, re.IGNORECASE):
        # Case 1: direct paper title in the query (e.g. "下载 BLAST: ... 这篇文章")
        direct = _extract_title_from_query(query)
        if direct and len(direct) > 8 and not re.search(r"第\s*\d+\s*条", direct):
            await message.channel.typing()
            await _do_single_paper_download(message, direct)
            return

        # Case 2: "下载第X条" — find most recent paper briefing in channel
        async for msg in message.channel.history(limit=50):
            if msg.id == message.id:
                continue
            if is_paper_message(msg.content) or extract_paper_titles(msg.content):
                await _handle_deep_fetch(msg, message)
                return
        await message.reply("⚠️ 未在近期消息中找到论文简报，请直接回复那条简报消息并 @我。")
        return

    log.info(f"Briefing query from {message.author}: {query[:80]}")
    await message.channel.typing()

    context = _build_context(query)
    if context:
        log.info(f"  Found briefing context ({len(context)} chars)")
    else:
        log.info("  No briefing context, using model knowledge")

    answer = await _call_ai_analysis(query, context)
    await _send_long(message.channel, answer, reference=message)


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info(f"Bot ready: {bot.user} | {len(bot.guilds)} guild(s)")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "📥":
        return
    if bot.user and payload.user_id == bot.user.id:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    if not is_paper_message(message.content):
        return

    log.info(f"📥 on message {payload.message_id}")
    await _handle_paper_download(message, channel)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if bot.user not in message.mentions:
        return

    if message.reference:
        # Reply + @mention → deep fetch
        try:
            original = await message.channel.fetch_message(message.reference.message_id)
        except discord.NotFound:
            await message.reply("找不到被回复的消息。")
            return
        await _handle_deep_fetch(original, message)
    else:
        # Standalone @mention → briefing search + AI analysis
        await _handle_briefing_query(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not DISCORD_BOT_TOKEN:
        log.error("DISCORD_BOT_TOKEN not set")
        sys.exit(1)
    log.info("Starting bot…")
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
