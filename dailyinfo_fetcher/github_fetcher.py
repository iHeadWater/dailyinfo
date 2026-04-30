"""Fetch GitHub repo info via API and format as a card."""
import re
from typing import Optional

import httpx

from .config import GITHUB_TOKEN
from .utils import get_logger

log = get_logger("github_fetcher")


def extract_github_repo(content: str) -> Optional[str]:
    """Extract owner/repo from content containing a github.com URL."""
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", content, re.IGNORECASE)
    if m:
        return m.group(1).rstrip("/")
    return None


def _fmt_number(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


async def fetch_github_card(repo: str) -> str:
    """Fetch repo metadata and README, return formatted card string."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        try:
            repo_resp = await client.get(f"https://api.github.com/repos/{repo}")
            repo_resp.raise_for_status()
            data = repo_resp.json()
        except Exception as e:
            log.warning(f"GitHub API error for {repo}: {e}")
            return f"⭐ **{repo}**\n🔗 https://github.com/{repo}\n（无法获取详情：{e}）"

        stars = _fmt_number(data.get("stargazers_count", 0))
        forks = _fmt_number(data.get("forks_count", 0))
        license_name = (data.get("license") or {}).get("spdx_id", "N/A")
        description = data.get("description") or ""
        language = data.get("language") or ""
        topics = data.get("topics", [])[:5]
        url = data.get("html_url", f"https://github.com/{repo}")

        # Try to get README
        readme_text = ""
        try:
            readme_resp = await client.get(
                f"https://api.github.com/repos/{repo}/readme",
                headers={**headers, "Accept": "application/vnd.github.raw"},
            )
            if readme_resp.status_code == 200:
                raw = readme_resp.text
                # Strip badges and HTML, keep first 800 chars
                raw = re.sub(r"!\[.*?\]\(.*?\)", "", raw)
                raw = re.sub(r"<[^>]+>", "", raw)
                raw = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", raw)
                readme_text = raw.strip()[:800]
        except Exception:
            pass

        # Build card
        lines = [
            f"⭐ **{data.get('name', repo)}**",
            f"📊 Stars: {stars} | Forks: {forks} | License: {license_name}",
        ]
        if language:
            lines.append(f"💻 语言：{language}")
        if description:
            lines.append(f"📝 {description}")
        if topics:
            lines.append(f"🏷 {' '.join(f'`{t}`' for t in topics)}")
        if readme_text:
            lines.append(f"\n🚀 README 节选：\n{readme_text[:600]}...")
        lines.append(f"\n🔗 {url}")

        return "\n".join(lines)
