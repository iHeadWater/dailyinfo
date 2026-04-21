#!/usr/bin/env python3
"""Discord bot — respond to @mentions with deep analysis of briefing content."""

import os
import re
import sys
import time
import glob
import json
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIEFINGS_DIR = os.path.expanduser("~/.openclaw/workspace/briefings")
PUSHED_DIR = os.path.expanduser("~/.openclaw/workspace/pushed")
DISCORD_API = "https://discord.com/api/v10"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
ANALYSIS_MODEL = "anthropic/claude-sonnet-4-5"
LOOKBACK_DAYS = 3  # search briefings from this many days back


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _load_env() -> dict:
    env = {}
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values
            env = dict(dotenv_values(env_path))
        except ImportError:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
    # environment variables override .env
    for k in ("DISCORD_BOT_TOKEN", "OPENROUTER_API_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = _load_env()
DISCORD_BOT_TOKEN = ENV.get("DISCORD_BOT_TOKEN", "")
OPENROUTER_API_KEY = ENV.get("OPENROUTER_API_KEY", "")

if not DISCORD_BOT_TOKEN:
    log("ERROR: DISCORD_BOT_TOKEN not set")
    sys.exit(1)
if not OPENROUTER_API_KEY:
    log("ERROR: OPENROUTER_API_KEY not set")
    sys.exit(1)

HEADERS_DISCORD = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Briefing search
# ---------------------------------------------------------------------------

def _recent_briefing_files() -> list[str]:
    """Return all briefing files from the past LOOKBACK_DAYS days."""
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    files = []
    for root_dir in (PUSHED_DIR, BRIEFINGS_DIR):
        for path in glob.glob(os.path.join(root_dir, "**", "*.md"), recursive=True):
            # extract date from filename like ..._2026-04-17...md
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
            if m:
                try:
                    fdate = datetime.strptime(m.group(1), "%Y-%m-%d")
                    if fdate >= cutoff:
                        files.append(path)
                except ValueError:
                    pass
    return files


def search_briefings(query: str) -> list[tuple[str, str]]:
    """Return (filename, relevant_excerpt) pairs that match the query."""
    keywords = [w.lower() for w in re.split(r"\s+", query.strip()) if len(w) > 1]
    results = []
    for path in _recent_briefing_files():
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        # skip empty placeholders
        if "📭 过去" in content and len(content.strip()) < 200:
            continue

        content_lower = content.lower()
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            results.append((score, path, content))

    # sort by relevance, return top 3
    results.sort(key=lambda x: x[0], reverse=True)
    return [(os.path.basename(p), c) for _, p, c in results[:3]]


def build_context(query: str) -> str:
    """Build a context block from matching briefing files."""
    matches = search_briefings(query)
    if not matches:
        return ""
    parts = []
    for fname, content in matches:
        # trim very long files to keep token usage reasonable
        trimmed = content[:3000] + ("\n...[内容过长已截断]" if len(content) > 3000 else "")
        parts.append(f"=== 来源: {fname} ===\n{trimmed}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# AI analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 DailyInfo 的科研情报分析助手。用户会 @ 你并提出问题或指定一篇论文/项目，
你要基于提供的简报内容进行深度解析，包括：核心贡献、方法原理、与领域现状的关系、潜在影响和局限性。
回答要专业、有深度，使用中文，适当使用 Markdown 格式。如果简报内容不足，也可以结合你自身的知识作答，但需注明。"""


def call_ai(user_query: str, context: str) -> str:
    if context:
        user_msg = f"用户问题：{user_query}\n\n以下是相关的每日简报内容，请基于此作答：\n\n{context}"
    else:
        user_msg = f"用户问题：{user_query}\n\n（未在近期简报中找到相关内容，请凭知识作答，并在回复末尾注明「注：未找到相关简报，以上为通用分析」。）"

    payload = {
        "model": ANALYSIS_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 2000,
    }
    resp = requests.post(
        OPENROUTER_API,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def discord_get(path: str) -> dict:
    resp = requests.get(f"{DISCORD_API}{path}", headers=HEADERS_DISCORD, timeout=10)
    resp.raise_for_status()
    return resp.json()


def discord_post(path: str, data: dict) -> dict:
    resp = requests.post(f"{DISCORD_API}{path}", headers=HEADERS_DISCORD, json=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


def split_message(text: str, max_len: int = 1950) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current:
                parts.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current:
        parts.append(current)
    return parts


def send_reply(channel_id: str, content: str, reference_msg_id: str | None = None) -> None:
    parts = split_message(content)
    for i, part in enumerate(parts):
        data: dict = {"content": part}
        if i == 0 and reference_msg_id:
            data["message_reference"] = {"message_id": reference_msg_id}
        discord_post(f"/channels/{channel_id}/messages", data)
        if len(parts) > 1:
            time.sleep(0.5)


def send_typing(channel_id: str) -> None:
    try:
        requests.post(
            f"{DISCORD_API}/channels/{channel_id}/typing",
            headers=HEADERS_DISCORD,
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Gateway (HTTP long-poll via Gateway v10)
# ---------------------------------------------------------------------------

def get_gateway_url() -> str:
    data = discord_get("/gateway/bot")
    return data["url"] + "?v=10&encoding=json"


# ---------------------------------------------------------------------------
# Main loop — uses Discord Gateway WebSocket
# ---------------------------------------------------------------------------

def run_bot() -> None:
    try:
        import websocket  # type: ignore
    except ImportError:
        log("ERROR: websocket-client not installed. Run: pip install websocket-client")
        sys.exit(1)

    me = discord_get("/users/@me")
    bot_id = me["id"]
    log(f"Logged in as {me['username']}#{me.get('discriminator','0')} (id={bot_id})")

    gw_url = get_gateway_url()
    log(f"Connecting to gateway: {gw_url}")

    heartbeat_interval: float = 0.0
    last_heartbeat: float = 0.0
    sequence: int | None = None

    def on_message(ws, raw):  # noqa: ARG001
        nonlocal heartbeat_interval, last_heartbeat, sequence

        try:
            msg = json.loads(raw)
        except Exception:
            return

        op = msg.get("op")
        data = msg.get("d")
        t = msg.get("t")
        s = msg.get("s")
        if s is not None:
            sequence = s

        # Hello — start heartbeating
        if op == 10:
            heartbeat_interval = data["heartbeat_interval"] / 1000
            ws.send(json.dumps({"op": 1, "d": sequence}))
            last_heartbeat = time.time()
            # Identify
            ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": DISCORD_BOT_TOKEN,
                    "intents": (1 << 9) | (1 << 15),  # GUILD_MESSAGES + MESSAGE_CONTENT
                    "properties": {"os": "linux", "browser": "dailyinfo", "device": "dailyinfo"},
                },
            }))

        # Heartbeat ACK — keep alive
        elif op == 11:
            last_heartbeat = time.time()

        # Dispatch
        elif op == 0 and t == "MESSAGE_CREATE":
            _handle_message(ws, data, bot_id)

    def on_error(ws, error):  # noqa: ARG001
        log(f"WebSocket error: {error}")

    def on_close(ws, code, reason):  # noqa: ARG001
        log(f"WebSocket closed: {code} {reason}")

    def on_open(ws):  # noqa: ARG001
        log("WebSocket connected")

    ws_app = websocket.WebSocketApp(
        gw_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    import threading

    def heartbeat_loop():
        while True:
            time.sleep(1)
            if heartbeat_interval > 0 and time.time() - last_heartbeat >= heartbeat_interval:
                try:
                    ws_app.send(json.dumps({"op": 1, "d": sequence}))
                except Exception:
                    pass

    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()

    log("Starting bot loop (Ctrl+C to stop)...")
    ws_app.run_forever(ping_interval=30, ping_timeout=10, reconnect=5)


def _handle_message(ws, data: dict, bot_id: str) -> None:  # noqa: ARG001
    # ignore bot's own messages
    author = data.get("author", {})
    if author.get("bot"):
        return

    content: str = data.get("content", "")
    channel_id: str = data["channel_id"]
    message_id: str = data["id"]

    # only respond when mentioned
    mentions = data.get("mentions", [])
    if not any(m["id"] == bot_id for m in mentions):
        return

    # strip the mention tag(s) from the query
    query = re.sub(r"<@!?\d+>", "", content).strip()
    if not query:
        send_reply(channel_id, "请告诉我你想深度解析哪篇论文或哪个内容 🔍", message_id)
        return

    log(f"Query from {author.get('username')}: {query[:80]}")
    send_typing(channel_id)

    try:
        context = build_context(query)
        if context:
            log(f"  Found context from briefings ({len(context)} chars)")
        else:
            log("  No matching briefings found, using model knowledge")

        answer = call_ai(query, context)
        send_reply(channel_id, answer, message_id)
        log("  Reply sent")
    except Exception as e:
        log(f"  ERROR: {e}")
        send_reply(channel_id, f"❌ 处理出错：{e}", message_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_bot()
