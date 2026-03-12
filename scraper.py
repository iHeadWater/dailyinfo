import feedparser
import hashlib
import json
import os
import requests
import time
from openai import OpenAI

# --- 配置区 ---
# 1. 数据源
DIRECT_SOURCES = [
    {"name": "Solidot官方", "url": "https://www.solidot.org/index.rss"},
    {"name": "V2EX官方", "url": "https://www.v2ex.com/index.xml"},
    {"name": "Arxiv-AI论文", "url": "https://rss.arxiv.org/rss/cs.AI"}
]

# 2. 语义过滤关键词（只有包含这些词的才发给 AI，省钱关键！）
KEYWORDS = ["AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "机器人"]

# 3. AI 配置 (从 GitHub Secrets 读取 Key)
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"), 
    base_url="https://api.deepseek.com"
)

def fetch_content():
    if os.path.exists("history.json"):
        with open("history.json", "r", encoding="utf-8") as f:
            try: history = json.load(f)
            except: history = []
    else:
        history = []

    new_stories = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for source in DIRECT_SOURCES:
        try:
            resp = requests.get(f"{source['url']}?t={int(time.time())}", headers=headers, timeout=20)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                for entry in feed.entries:
                    item_id = hashlib.md5(entry.link.encode()).hexdigest()
                    if item_id not in history:
                        # 核心节流：只保留包含关键词的文章送往 AI
                        title = entry.title
                        if any(key.lower() in title.lower() for key in KEYWORDS):
                            new_stories.append({
                                "title": title,
                                "link": entry.link,
                                "source": source['name']
                            })
                        history.append(item_id)
        except Exception as e:
            print(f"抓取 {source['name']} 失败: {e}")

    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f)
    
    return new_stories

def summarize_news(news_list):
    if not news_list:
        return "今日暂无符合硬核标准的 AI 技术动态。"
    
    # 构造素材，限制前 20 条最相关的，进一步省 Token
    raw_text = "\n".join([f"- {n['title']} (来自: {n['source']})" for n in news_list[:20]])
    
    prompt = f"""
    你现在是《ai水文信息战》主编。请根据以下素材撰写一段今日简报。
    风格要求：极客感、宏大叙事、信息密度高。
    参考文风：'2026年的春天，AI圈的关键词不再是“聊天”，而是“接管”...'
    
    内容要求：
    1. 提取一个今日最核心的技术趋势。
    2. 挑选 2-3 个项目进行点评，强调其'物理直觉'或'生产力进化'。
    3. 语言要干练，拒绝废话。

    素材内容：
    {raw_text}
    """
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AI 提炼失败: {e}"

if __name__ == "__main__":
    new_data = fetch_content()
    print(f"筛选出 {len(new_data)} 条硬核资讯，正在生成 AI 简报...")
    
    report = summarize_news(new_data)
    print("\n" + "="*30 + " AI 简报 " + "="*30)
    print(report)
    print("="*69)
