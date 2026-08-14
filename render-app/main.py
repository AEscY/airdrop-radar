import os
import json
import asyncio
import httpx
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager

# ========== 日志配置 ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ========== 全局变量 ==========
bot_initialized = False
scan_count = 0
last_scan_result = "Pending..."

# ========== 模拟你的环境 ==========
# 如果你的真实库是 python-telegram-bot，把下面的 send_telegram 函数换成它的用法
# 这里先用 httpx 模拟发送，确保你能立刻收到消息
async def send_telegram(message: str):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    
    if not token or not chat_id:
        logger.error("❌ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info(f"✅ [Telegram] 发送成功: {message[:30]}...")
            else:
                logger.error(f"❌ [Telegram] 发送失败: {resp.text}")
    except Exception as e:
        logger.error(f"❌ [Telegram] 发送异常: {e}")

# ========== 核心业务逻辑 ==========
async def fetch_data():
    """抓取 Alphadrops 数据"""
    url = "https://api.alphadrops.net/drops"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=15)
            data = r.json()
            return data.get("drops", [])
    except Exception as e:
        logger.error(f"❌ 抓取数据失败: {e}")
        return []

async def run_radar_logic():
    """雷达扫描逻辑"""
    global scan_count, last_scan_result
    
    logger.info("⚙️ 开始执行空投雷达扫描...")
    scan_count += 1
    
    drops = await fetch_data()
    high_value = []
    
    for d in drops:
        score = d.get("score", 0)
        claimable = d.get("isClaimable", False)
        name = d.get("name", "N/A")
        
        # 筛选逻辑：分数大于 70 且可领取
        if score >= 70 and claimable:
            high_value.append({
                "name": name,
                "score": score,
                "chain": d.get("blockchain", "N/A"),
                "url": f"https://alphadrops.net/drops/{d.get('slug')}"
            })

    # 推送消息
    if high_value:
        msg = "🔥 <b>【高价值空投预警】</b>

"
        for item in high_value[:5]:
            msg += f"<b>{item['name']}</b>
"
            msg += f• 评分: <code>{item['score']}</code> | 链: {item['chain']}
"
            msg += f"<a href='{item['url']}'>👉 点击直达领取</a>

"
        await send_telegram(msg)
        last_scan_result = f"发现 {len(high_value)} 个高价值空投"
        logger.info(last_scan_result)
    else:
        last_scan_result = "未发现符合条件的高价值空投"
        logger.info(last_scan_result)

# ========== Lifespan 管理 (替代多线程) ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- 启动时执行 ---
    logger.info("🚀 ===== Airdrop Radar 启动中 =====")
    
    # 1. 发送上线通知
    await send_telegram("🟢 <b>系统已上线</b>
空投雷达服务已启动，开始为你监控最新空投。")
    
    # 2. 启动定时扫描任务 (使用 asyncio 原生任务，避开多线程陷阱)
    async def periodic_task():
        while True:
            await run_radar_logic()
            await asyncio.sleep(900) # 15分钟
            
    task = asyncio.create_task(periodic_task())
    logger.info("⏰ 定时扫描任务已在后台运行 (每15分钟一次)")
    yield
    # --- 关闭时执行 ---
    task.cancel()
    logger.info("🛑 服务关闭，定时任务已停止")

# ========== FastAPI 应用 ==========
app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {
        "status": "alive", 
        "scans": scan_count, 
        "last": last_scan_result,
        "uptime": datetime.now(timezone.utc).isoformat()
    }

@app.post("/collect")
async def collect(request: Request):
    """供 GitHub Actions 或手动触发的接口"""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    
    logger.info("🚀 收到 /collect 手动触发请求")
    await run_radar_logic()
    return {"result": "Manual scan completed"}

@app.get("/")
async def root():
    return {"service": "Airdrop Radar", "status": "running", "docs": "/docs"}

# ========== 本地测试入口 ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
