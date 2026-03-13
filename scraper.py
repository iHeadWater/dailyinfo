import os
import requests
import json
import datetime

# --- 配置区 ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

def upload_to_notion(content, title):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # 🌟 免费自动搜图
    cover_image_url = "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?auto=format&fit=crop&q=80&w=1000"

    # 构造内容块（含自动切片逻辑）
    chunk_size = 1000
    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    
    children_blocks = [{
        "object": "block",
        "type": "image",
        "image": { "type": "external", "external": { "url": cover_image_url } }
    }]
    
    for chunk in chunks:
        children_blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": { "rich_text": [{"type": "text", "text": {"content": chunk}}] }
        })

    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": { "Name": {"title": [{"text": {"content": title}}]} },
        "children": children_blocks
    }
    
    res = requests.post(url, headers=headers, json=payload)
    return res.status_code == 200

def main():
    # 1. 这里的抓取逻辑保持你原来的不变，确保数据进入 weekly_pool.json
    print("正在执行每日素材采集...")
    # fetch_rss_to_pool() # 假设这是你原来的抓取函数名
    
    # 2. 判断日期：0 是周一
    # 注意：北京时间周一早上 8:00 对应 UTC 周一 0:00
    weekday = datetime.datetime.now().weekday()
    
    if weekday == 0: 
        print("🚀 检测到今天是周一，正在汇总本周素材并生成周报...")
        
        # 读取池子里的内容
        with open('weekly_pool.json', 'r', encoding='utf-8') as f:
            pool = json.load(f)
        
        if pool:
            # 调用 DeepSeek 生成总结（逻辑保持你原来的不变）
            # report = generate_with_deepseek(pool)
            
            # 发送 Notion
            title = f"ai水文信息战 · 周刊 ({datetime.date.today()})"
            # 假设 report 是你生成的总结文字
            success = upload_to_notion(report, title)
            
            if success:
                print("✅ 周报已送达 Notion！正在清空素材池...")
                with open('weekly_pool.json', 'w', encoding='utf-8') as f:
                    json.dump([], f) # 阅后即焚，重置池子
            else:
                print("❌ 发送 Notion 失败，素材保留在池子中。")
        else:
            print("本周没有新素材。")
    else:
        print(f"今天周{weekday+1}，仅完成素材入库，周一统一发布。")

if __name__ == "__main__":
    # --- 【测试模式：全量强制运行】 ---
    print("🚀 测试模式启动：正在强制抓取并同步...")
    
    # 1. 抓取逻辑（由于 history.json 之前可能记住了链接，
    # 如果你想强制重抓这周所有，可以手动在 GitHub 删掉 history.json 再跑）
    current_pool = fetch_and_pool()
    
    # 2. 无论今天周几，都强制触发发送
    if not current_pool:
        print("⚠️ 警告：池子仍然是空的，可能是 RSS 源没有匹配到关键字。")
    else:
        print(f"📦 当前池子共有 {len(current_pool)} 条素材，准备出货...")
        
        # 3. 生成报告
        report = summarize_weekly(current_pool)
        
        # 4. 发送 Notion
        bj_date = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
        upload_to_notion(report, f"【测试发布】AI水文周报 | {bj_date}")
        
        # 5. 测试期间为了反复看效果，可以【暂时不清空】池子
        # 等你觉得排版完美了，再把下面这行取消注释，或者手动改回正式版逻辑
        # with open("weekly_pool.json", "w") as f: json.dump([], f)
        
        print("✨ 测试任务完成！请检查 Notion。")
