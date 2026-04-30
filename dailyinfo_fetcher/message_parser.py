"""Parse Discord messages: detect type, extract items, resolve user intent."""
import json
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import OPENROUTER_API_KEY
from .utils import get_logger

log = get_logger("message_parser")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = "anthropic/claude-sonnet-4-5"


@dataclass
class ParsedItem:
    index: int
    title: str
    body: str


def is_paper_message(content: str) -> bool:
    return bool(re.search(r"🗂.+今日简报", content))


def is_github_related(content: str) -> bool:
    return bool(re.search(r"github\.com/[\w.-]+/[\w.-]+", content, re.IGNORECASE))


def extract_paper_titles(content: str) -> list[str]:
    return re.findall(r"\d+\.\s+\*\*(.+?)\*\*", content)


def extract_items(content: str) -> list[ParsedItem]:
    items = []
    pattern = re.compile(
        r"(\d+)\.\s+(?:\*\*(.+?)\*\*\s*)?\n?\s*(.*?)(?=\n\d+\.|\n##|\Z)",
        re.DOTALL,
    )
    for m in pattern.finditer(content):
        idx = int(m.group(1))
        title = (m.group(2) or "").strip()
        body = (m.group(3) or "").strip()
        if title or body:
            items.append(ParsedItem(index=idx, title=title, body=body))
    return items


def resolve_intent_local(user_query: str, items: list[ParsedItem]) -> Optional[ParsedItem]:
    m = re.search(r"第\s*(\d+)\s*条|^(\d+)$", user_query.strip())
    if m:
        idx = int(m.group(1) or m.group(2))
        for item in items:
            if item.index == idx:
                return item
    return None


async def resolve_intent_claude(original_content: str, user_query: str) -> tuple[str, str]:
    if not OPENROUTER_API_KEY:
        return user_query, user_query

    prompt = (
        "以下是一条推送消息的内容：\n\n"
        f"{original_content[:3000]}\n\n"
        "---\n"
        f"用户说：「{user_query}」\n\n"
        "请：\n"
        "1. 从消息中找出用户想了解的那一条内容的完整标题或描述\n"
        "2. 提取3-5个最适合用于网络搜索的英文关键词\n\n"
        "以JSON格式回复：\n"
        '{"item_title": "...", "search_query": "..."}'
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 300},
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return data.get("item_title", user_query), data.get("search_query", user_query)
    except Exception as e:
        log.warning(f"Claude intent parse failed: {e}")
    return user_query, user_query
