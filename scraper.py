import feedparser
import hashlib
import json
import os
import requests
import time

# 1. 配置你需要监听的 RSS 路径 (这里以科技资讯 Solidot 和 V2EX 为例)
# 你可以根据 RSSHub 的文档随意增加路径
RSS_PATHS = [
    "/solidot/main",
    "/v2ex/topics/latest"
]

# 2. RSSHub 镜像池：如果主站 rsshub.app 访问不了，自动切换到镜像站
MIRRORS = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.blog",
    "https://rsshub.net"
]

def fetch_with_retry():
    # A. 加载历史记忆 (history.json)
    if os.path.exists("history.json"):
        with open("history.json", "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except:
                history = []
    else:
        history = []

    new_stories = []
    
    # B. 遍历所有预设的 RSS 路径
    for path in RSS_PATHS:
        # C. 对每个路径尝试不同的镜像站
        for base in MIRRORS:
            try:
                url = f"{base}{path}"
                print(f"正在尝试抓取: {url}")
                
                # 模拟浏览器请求头
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                resp = requests.get(url, headers=headers, timeout=20)
                
                if resp.status_code == 200:
                    feed = feedparser.parse(resp.text)
                    for entry in feed.entries:
                        # D. 核心去重逻辑：使用链接的 MD5 作为唯一 ID
                        item_id = hashlib.md5(entry.link.encode()).hexdigest()
                        
                        if item_id not in history:
                            new_stories.append({
                                "title": entry.title,
                                "link": entry.link,
                                "summary": entry.get("description", entry.get("summary", ""))
                            })
                            history.append(item_id)
                    
                    print(f"成功从 {base} 获取数据")
                    break # 抓取成功，跳出镜像循环，换下一个 RSS 路径
            except Exception as e:
                print(f"镜像 {base} 连接失败: {e}")
                continue
        
        # 抓取间隔，防止被封 IP
        time.sleep(2)

    # E. 更新记忆文件，只保留最近 1000 条，防止 history.json 无限增大
    with open("history.json", "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f)
    
    return new_stories

if __name__ == "__main__":
    new_data = fetch_with_retry()
    print(f"\n--- 任务完成 ---")
    print(f"本次共发现 {len(new_data)} 条新资讯。")
    
    # 下一步我们将在这里对接 DeepSeek/Gemini API 进行语义提炼
    for idx, item in enumerate(new_data[:5]): 
        print(f"[{idx+1}] {item['title']}")
