import feedparser
import hashlib
import json
import os
import requests
import datetime
from openai import OpenAI

# --- 配置区 ---
KEYWORDS = ["AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "AI4S", "算法", "Cursor", "编程"]
client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")

def fetch_and_pool():
    history = json.load(open("history.json", "r")) if os.path.exists("history.json") else []
    pool = json.load(open("weekly_pool.json", "r")) if os.path.exists("weekly_pool.json") else []
    sources = [
        {"name": "Solidot", "url": "https://www.solidot.org/index.rss"},
        {"name": "V2EX", "url": "https://www.v2ex.com/index.xml"},
        {"name": "Arxiv-AI", "url": "https://rss.arxiv.org/rss/cs.AI"}
    ]
    new_found = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    for s in sources:
        try:
            resp = requests.get(s['url'], headers=headers, timeout=20)
            feed = feedparser.parse(resp.text)
            for entry in feed.entries:
                item_id = hashlib.md5(entry.link.encode()).hexdigest()
                if item_id not in history:
                    if any(key.lower() in entry.title.lower() for key in KEYWORDS):
                        new_item = {"title": entry.title, "link": entry.link, "source": s['name']}
                        pool.append(new_item)
                    history.append(item_id)
        except: continue
    with open("history.json", "w") as f: json.dump(history[-1000:], f)
    with open("weekly_pool.json", "w") as f: json.dump(pool, f, ensure_ascii=False)
    return pool

def summarize_weekly(pool):
    if not pool: return "本周池子是空的。"
    beijing_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y/%m/%d')
    
    # 简单的分类逻辑
    models = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["模型", "GPT", "LLM", "DeepSeek"])]
    tools = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["Agent", "工具", "Cursor", "OpenClaw"])]
    science = [n for n in pool if any(k.lower() in n['title'].lower() for k in ["科学", "AI4S", "水文", "生物"])]

    def get_text(l): return "\n".join([f"- {i['title']}" for i in l[:20]]) if l else "暂无核心波动。"

    prompt = f"""
    你现在是《ai水文信息战》主笔。今天是 {beijing_time}。
    请撰写本周周报。使用 Markdown 格式，板块标题用 ### 搭配 Emoji。
    素材：
    [模型]：{get_text(models)}
    [工具]：{get_text(tools)}
    [科学]：{get_text(science)}
    """
    response = client.chat.completions.create(model="deepseek-chat", messages=[{"role": "user", "content": prompt}], max_tokens=2000, temperature=0.8)
    return response.choices[0].message.content

def upload_to_notion(content, title):
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DATABASE_ID")
    url = "https://api.api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    # 将长文按 2000 字符切块，防止 Notion API 报错
    blocks = []
    for i in range(0, len(content), 2000):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": content[i:i+2000]}}]
            }
        })

    payload = {
        "parent": {"database_id": db_id},
        "properties": {"Name": {"title": [{"text": {"content": title}}]}},
        "children": blocks
    }
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code == 200:
        print("✅ 熟食已送达 Notion！")
    else:
        print(f"❌ 同步失败: {res.text}")

if __name__ == "__main__":
    current_pool = fetch_and_pool()
    # 【测试模式】：强行出货
    print(f"--- 正在提炼素材 ---")
    report = summarize_weekly(current_pool)
    print(report)
    
    bj_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
    upload_to_notion(report, f"AI水文周报 | {bj_date}")
