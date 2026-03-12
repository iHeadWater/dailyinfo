import feedparser
import hashlib
import json
import os
import requests
import time

# 1. 核心源配置：混合官方直连源 + 极稳镜像源
# 我们直接使用官方原生 RSS 链接，成功率接近 100%
DIRECT_SOURCES = [
    {"name": "Solidot官方", "url": "https://www.solidot.org/index.rss"},
    {"name": "V2EX官方", "url": "https://www.v2ex.com/index.xml"},
    {"name": "Arxiv-AI论文", "url": "https://rss.arxiv.org/rss/cs.AI"}
]

# 2. 备用镜像（仅用于必须通过 RSSHub 转换的源）
RSSHUB_MIRRORS = [
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com"
]

def fetch_content():
    # A. 加载历史记忆
    if os.path.exists("history.json"):
        with open("history.json", "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except:
                history = []
    else:
        history = []

    new_stories = []
    # 模拟真实浏览器，防止被服务器拒绝访问
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    # B. 开始抓取官方直连源
    for source in DIRECT_SOURCES:
        try:
            print(f"正在直连抓取: {source['name']} ({source['url']})")
            # 增加随机参数绕过缓存
            resp = requests.get(f"{source['url']}?t={int(time.time())}", headers=headers, timeout=20)
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries:
                    item_id = hashlib.md5(entry.link.encode()).hexdigest()
                    if item_id not in history:
                        new_stories.append({
                            "title": entry.title,
                            "link": entry.link,
                            "source": source['name'],
                            "summary": entry.get("description", entry.get("summary", ""))
                        })
                        history.append(item_id)
                        count += 1
                print(f"✅ {source['name']} 抓取成功，发现 {count} 条新内容")
        except Exception as e:
            print(f"❌ {source['name']} 连不上: {e}")
        time.sleep(1)

    # C. 更新记忆文件
    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f)
    
    return new_stories

if __name__ == "__main__":
    new_data = fetch_content()
    print(f"\n--- 任务完成 ---")
    print(f"本次共发现 {len(new_data)} 条新资讯。")
    
    for idx, item in enumerate(new_data[:10]): 
        print(f"[{idx+1}] [{item['source']}] {item['title']}")
