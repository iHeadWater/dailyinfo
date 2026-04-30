"""News deep-fetch: search → Jina Reader → AI summary via OpenRouter."""
import httpx

from .config import OPENROUTER_API_KEY, TAVILY_API_KEY, JINA_API_KEY
from .utils import get_logger

log = get_logger("news_fetcher")

JINA_BASE = "https://r.jina.ai/"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = "anthropic/claude-sonnet-4-5"


async def _call_ai(prompt: str, max_tokens: int = 1000) -> str:
    if not OPENROUTER_API_KEY:
        return ""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def _search_tavily(query: str) -> list[dict]:
    try:
        from tavily import AsyncTavilyClient
        client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
        resp = await client.search(query, max_results=5)
        return resp.get("results", [])
    except Exception as e:
        log.warning(f"Tavily search failed: {e}")
        return []


async def _search_duckduckgo(query: str) -> list[dict]:
    try:
        from ddgs import DDGS
        import asyncio
        loop = asyncio.get_event_loop()
        def _sync_search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        results = await loop.run_in_executor(None, _sync_search)
        return [{"title": r["title"], "url": r["href"], "content": r["body"]} for r in results]
    except Exception as e:
        log.warning(f"DuckDuckGo search failed: {e}")
        return []


async def search_web(query: str) -> list[dict]:
    if TAVILY_API_KEY:
        results = await _search_tavily(query)
        if results:
            return results
    return await _search_duckduckgo(query)


async def fetch_with_jina(url: str) -> str:
    jina_url = f"{JINA_BASE}{url}"
    headers = {"Accept": "text/plain"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(jina_url, headers=headers)
            return resp.text[:8000]
    except Exception as e:
        log.warning(f"Jina fetch failed for {url}: {e}")
        return ""


async def generate_summary(item_title: str, search_results: list[dict], page_content: str) -> str:
    sources = [r.get("url", "") for r in search_results[:3] if r.get("url")]
    source_names = [r.get("title", "") for r in search_results[:3] if r.get("title")]

    if not OPENROUTER_API_KEY:
        # No API key: return formatted search snippets directly
        lines = [f"🔍 **{item_title}**\n"]
        for r in search_results[:3]:
            lines.append(f"📰 **{r.get('title', '')}**\n🔗 {r.get('url', '')}\n{r.get('content', '')[:300]}\n")
        return "\n".join(lines)

    context = ""
    if page_content and len(page_content) > 100:
        context = f"\n\n以下是原文内容（节选）：\n{page_content[:5000]}"
    elif search_results:
        snippets = "\n".join(
            f"- {r.get('title', '')}: {r.get('content', '')[:500]}"
            for r in search_results[:3]
        )
        context = f"\n\n以下是搜索结果摘要：\n{snippets}"

    prompt = (
        f"请对以下内容生成一份详细的中文总结（约500字），面向科研人员，突出核心要点、方法和影响：\n"
        f"主题：{item_title}{context}"
    )

    try:
        summary = await _call_ai(prompt, max_tokens=1000)
    except Exception as e:
        log.error(f"AI summary failed: {e}")
        summary = f"（AI 总结暂时不可用：{e}）"

    source_line = " / ".join(source_names[:2]) if source_names else "网络搜索"
    url_line = "\n".join(f"🔗 {u}" for u in sources[:2]) if sources else ""
    reading_links = "\n".join(f"- {u}" for u in sources[2:]) if len(sources) > 2 else ""

    parts = [f"🔍 **{item_title}**\n", f"📰 来源：{source_line}"]
    if url_line:
        parts.append(url_line)
    parts.append(f"\n📋 详细内容：\n{summary}")
    if reading_links:
        parts.append(f"\n💡 延伸阅读：\n{reading_links}")

    return "\n".join(parts)


async def fetch_news_deep(item_title: str, search_query: str) -> str:
    log.info(f"Fetching news deep: {search_query}")
    results = await search_web(search_query)
    log.info(f"  Got {len(results)} search results")

    page_content = ""
    if results:
        top_url = results[0].get("url", "")
        if top_url:
            log.info(f"  Fetching via Jina: {top_url}")
            page_content = await fetch_with_jina(top_url)
            log.info(f"  Jina returned {len(page_content)} chars")

    return await generate_summary(item_title, results, page_content)
