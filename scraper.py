import feedparser
import hashlib
import json
import os
import requests
import datetime
from openai import OpenAI

# --- 配置区 ---
# 关键词匹配，确保涵盖 AI 和水文科学
KEYWORDS = ["AI", "智能体", "Agent", "模型", "架构", "水文", "科学", "OpenClaw", "DeepSeek", "GPT", "Claude", "AI4S", "算法", "Cursor", "编程"]
client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")

def fetch_and_pool():
    """抓取素材并存入周池子"""
    # 【测试模式】：暂时不读取 history，确保你能抓到这周所有内容
    pool = [] 
    sources = [
        {"name": "Solidot", "url": "https://www.solidot.org/index.rss"},
        {"name": "V2EX", "url": "https://www.v2ex.com/index.xml"},
        {"name": "Arxiv-AI", "url": "https://rss.arxiv.org/rss/cs.AI"}
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    print("开始抓取 RSS 源...")
    for s in sources:
        try:
            resp = requests.get(s['url'], headers=headers, timeout=20)
            feed = feedparser.parse(resp.text)
            for entry in feed.entries:
                # 关键词过滤
                if any(key.lower() in entry.title.lower() for key in KEYWORDS):
                    new_item = {"title": entry.title, "link": entry.link, "source": s['name']}
                    pool.append(new_item)
        except Exception as e:
            print(f"抓取 {s['name']} 失败: {e}")
            continue
    
    # 将抓到的存入池子文件
    with open("weekly_pool.json", "w", encoding='utf-8') as f: 
        json.dump(pool, f, ensure_ascii=False, indent=2)
    return pool

def summarize_weekly(pool):
    """召唤 DeepSeek 进行周度汇总"""
    if not pool: return None
    
    bj_time = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y/%m/%d')
    # 提取标题作为素材
    text_content = "\n".join([f"- {i['title']} (来源: {i['source']})" for i in pool[:30]])
    
    prompt = f"""
    你现在是《ai水文信息战》主笔。今天是 {bj_time}。
    请根据以下本周收集的素材撰写一份深度周报。
    要求：
    1. 使用 Markdown 格式。
    2. 板块标题使用 ### 搭配相关的 Emoji。
    3. 语言风格要硬核、充满科技感和洞察力。
    4. 结尾加上一段对“AI+水文”未来的简短寄语。

    素材列表：
    {text_content}
    """
    
    print("正在召唤 DeepSeek 生成周报...")
    response = client.chat.completions.create(
        model="deepseek-chat", 
        messages=[{"role": "user", "content": prompt}], 
        max_tokens=2000, 
        temperature=0.8
    )
    return response.choices[0].message.content

def upload_to_notion(content, title):
    """将生成的周报上传至 Notion，带自动配图和分段逻辑"""
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DATABASE_ID")
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    # 1. 自动配图（Unsplash 科技感大图）
    cover_image_url = "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&q=80&w=1000"
    
    # 2. 文本切片（防止 Notion 400 错误）
    chunk_size = 1000
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    
    # 3. 构造子块
    children_blocks = []
    # 插入封面图
    children_blocks.append({
        "object": "block",
        "type": "image",
        "image": { "type": "external", "external": { "url": cover_image_url } }
    })
    
    # 插入正文
    for chunk in chunks:
        children_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": { "rich_text": [{"type": "text", "text": {"content": chunk}}] }
        })

    payload = {
        "parent": {"database_id": db_id},
        "properties": { "Name": {"title": [{"text": {"content": title}}]} },
        "children": children_blocks
    }
    
    res = requests.post(url, headers=headers, json=payload)
    return res.status_code == 200

if __name__ == "__main__":
    # 1. 每天都会执行抓取并存入 weekly_pool.json
    print("--- 每日素材采集开始 ---")
    current_pool = fetch_and_pool() 
    
    # 2. 获取北京时间判断周几
    bj_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    weekday = bj_time.weekday() # 0 是周一
    
    print(f"当前北京时间: {bj_time.strftime('%Y-%m-%d %H:%M')}, 星期{weekday+1}")

    # 3. 只有周一 08:30 那次运行会进入发布逻辑
    if weekday == 0: 
        print("🚀 检测到周一发布日，正在生成周报...")
        if current_pool:
            report = summarize_weekly(current_pool)
            success = upload_to_notion(report, f"AI水文周报 | {bj_time.strftime('%Y-%m-%d')}")
            
            if success:
                # 只有成功发布到 Notion 才清空池子
                with open("weekly_pool.json", "w", encoding='utf-8') as f:
                    json.dump([], f)
                print("🔥 本周素材池已阅后即焚，开启新一周循环。")
        else:
            print("⚠️ 池子是空的，无法生成周报。")
    else:
        print(f"今天星期{weekday+1}，仅完成素材采集。周一早上 08:30 准时交稿！")
