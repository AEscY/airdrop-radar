import os
import httpx
import asyncio
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Optional

# ------------------- 配置区 -------------------
app = FastAPI()
logger = lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# 模拟数据库（生产环境请换成 Redis 或 SQLite）
USER_WALLETS: Dict[str, str] = {}  # chat_id -> wallet_address
SCAN_LOCK = False # 防止并发扫描导致 API 超限

# ------------------- 核心类定义 -------------------
class AirdropItem(BaseModel):
    name: str
    chain: str
    score: int
    status: str
    action_url: str = ""

# ------------------- 模拟爬虫逻辑 -------------------
async def mock_scraper(wallet: Optional[str] = None) -> list[AirdropItem]:
    """模拟雷达扫描，如果有 wallet 则模拟“命中”"""
    await asyncio.sleep(2) # 模拟网络延迟
    
    items = [
        AirdropItem(name="MetaMask", chain="Ethereum, Ink", score=70, status="可领取"),
        AirdropItem(name="ZKSync", chain="zkSync Era", score=65, status="待交互"),
    ]
    
    # 模拟：如果用户绑定了钱包，高亮显示特定项目
    if wallet:
        items.append(AirdropItem(name=f"专属大毛 ({wallet[:6]}...)", chain="Arbitrum", score=95, status="🔥 高优可领", action_url="https://example.com"))
        
    return items

# ------------------- Telegram API 封装 -------------------
async def send_telegram(chat_id: str, text: str, reply_markup=None):
    token = os.environ.get("TG_BOT_TOKEN")
    if not token: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": reply_markup}
            )
    except Exception as e:
        logger(f"发送消息失败: {e}")

async def edit_telegram_message(chat_id: str, message_id: int, text: str, reply_markup=None):
    token = os.environ.get("TG_BOT_TOKEN")
    if not token: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown", "reply_markup": reply_markup}
            )
    except Exception as e:
        logger(f"编辑消息失败: {e}")

# ------------------- 菜单构建器 -------------------
def build_main_keyboard(has_wallet: bool):
    """动态构建键盘：如果没绑钱包，优先提示绑定"""
    keyboard = []
    if not has_wallet:
        keyboard.append([{"text": "🔗 绑定钱包 (核心)", "callback_data": "cmd_bind"}])
    else:
        keyboard.append([{"text": "🔍 立即扫描", "callback_data": "cmd_scan"}])
        keyboard.append([{"text": "💰 我的资产", "callback_data": "cmd_balance"}])
        
    keyboard.append([{"text": "⚙️ 设置过滤", "callback_data": "cmd_settings"}])
    keyboard.append([{"text": "❓ 帮助", "callback_data": "cmd_help"}])
    return {"inline_keyboard": keyboard}

# ------------------- 命令处理逻辑 -------------------
class TelegramUpdate(BaseModel):
    message: Optional[dict] = None
    callback_query: Optional[dict] = None

@app.post("/webhook")
async def telegram_webhook(update: TelegramUpdate):
    global SCAN_LOCK
    
    data = update.dict()
    msg = data.get("message")
    callback = data.get("callback_query")
    token = os.environ.get("TG_BOT_TOKEN")
    
    if not token: raise HTTPException(status_code=500, detail="Token not set")

    # 1. 处理文本命令
    if msg and msg.get("text"):
        chat_id = msg["chat"]["id"]
        text = msg["text"]
        
        if text == "/start":
            await send_telegram(chat_id, 
                "👋 *空投雷达 Pro 版*
*升级亮点：支持钱包绑定 & 精准推送*
                
点击下方按钮开始：", 
                build_main_keyboard(False)
            )
            
        elif text == "/bind":
            USER_WALLETS[str(chat_id)] = "0x123...abc" # 模拟绑定
            await send_telegram(chat_id, "✅ 钱包模拟绑定成功！现在开始为您精准扫描。", build_main_keyboard(True))

    # 2. 处理按钮回调 (核心交互逻辑)
    elif callback:
        query = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        msg_id = callback["message"]["message_id"]
        
        # --- 场景 A：用户点击了“立即扫描” ---
        if query == "cmd_scan":
            has_wallet = str(chat_id) in USER_WALLETS
            
            # 1. 立即回复，给用户反馈（防止超时）
            await edit_telegram_message(chat_id, msg_id, "⏳ *正在全链扫描... 请稍候* ⏳")
            
            # 2. 异步执行耗时任务（这里用 asyncio.sleep 模拟爬虫）
            # 注意：在真实环境中，这里应该使用 Background Tasks 或将任务放入队列
            await asyncio.sleep(2) 
            results = await mock_scraper(USER_WALLETS.get(str(chat_id)))
            
            # 3. 格式化结果
            if not results:
                reply = "🔍 *扫描完成*
未发现新的高价值空投。"
            else:
                reply = "🔍 *扫描完成！发现新机会：*

"
                for item in results:
                    reply += f"*[ {item.score}分 ]* `{item.name}`\n"
                    reply += f"链: {item.chain} | 状态: {item.status}
"
                    if item.action_url:
                        reply += f"[👉 立即操作]({item.action_url})
"
                    reply += "
"
            
            # 4. 更新消息内容为结果
            await edit_telegram_message(chat_id, msg_id, reply, build_main_keyboard(has_wallet))

        # --- 场景 B：用户点击了“绑定钱包” ---
        elif query == "cmd_bind":
            await edit_telegram_message(chat_id, msg_id, 
                "🔗 *钱包绑定向导*
请提供您的 EVM 兼容地址 (0x...)：
*(注：目前为演示模式，输入任意内容即视为绑定)*", 
                {"inline_keyboard": [[{"text": "取消", "callback_data": "cmd_cancel"}]]}
            )
            # 真实场景下，这里需要设置一个状态标记，等待用户下一条消息

        # --- 场景 C：其他菜单 ---
        elif query == "cmd_help":
            await edit_telegram_message(chat_id, msg_id, 
                "📖 *帮助中心*
`/start` - 启动雷达
`/bind` - 绑定钱包获取专属信号
`🔍 立即扫描` - 手动刷新数据"
            )

    return JSONResponse(content={"ok": True})

# ------------------- 生命周期管理 -------------------
@app.on_event("startup")
async def startup_event():
    logger("🚀 雷达 Pro 启动成功！")
    # 设置 Webhook 逻辑可以在这里补充

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
