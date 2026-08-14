import os
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 全局变量 ---
scan_count = 0
last_scan_result = "暂无扫描记录"

# --- 配置 ---
TELEGRAM_API = "https://api.telegram.org/bot{}/"
TELEGRAM_SET_WEBHOOK = TELEGRAM_API + "setWebhook"
TELEGRAM_SEND_MESSAGE = TELEGRAM_API + "sendMessage"
TELEGRAM_EDIT_MESSAGE = TELEGRAM_API + "editMessageText"
TELEGRAM_DELETE_MESSAGE = TELEGRAM_API + "deleteMessage"

# ===== 模拟扫描逻辑 (占位) =====
async def run_radar_and_push():
    """模拟抓取空投的逻辑"""
    global scan_count, last_scan_result
    scan_count += 1
    logger.info(f"🔍 执行第 {scan_count} 次模拟扫描...")
    
    # 模拟耗时
    await asyncio.sleep(2)
    
    # 模拟结果 (随机一下增加真实感)
    import random
    found_count = random.randint(0, 3)
    last_scan_result = f"扫描34个项目，发现{found_count}个高价值空投"
    
    return scan_count, last_scan_result

# ===== Telegram API 封装 =====
async def send_telegram(token: str, chat_id: str, text: str, reply_markup=None):
    async with httpx.AsyncClient(timeout=10) as client:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_notification": False
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
            
        resp = await client.post(TELEGRAM_SEND_MESSAGE.format(token=token), json=payload)
        resp.raise_for_status()
        return resp.json()

async def edit_telegram_message(token: str, chat_id: str, message_id: int, text: str, reply_markup=None):
    async with httpx.AsyncClient(timeout=10) as client:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await client.post(TELEGRAM_EDIT_MESSAGE.format(token=token), json=payload)

# ===== 命令处理逻辑 =====
async def handle_telegram_command(body: dict):
    global scan_count, last_scan_result
    
    try:
        message = body.get("message")
        callback_query = body.get("callback_query")
        
        # 获取 Token 和 Chat ID
        token = os.environ.get("TG_BOT_TOKEN")
        chat_id = os.environ.get("TG_CHAT_ID")
        
        if not token or not chat_id:
            logger.error("环境变量 TG_BOT_TOKEN 或 TG_CHAT_ID 未设置")
            return

        # --- 1. 处理按钮回调 (图二面板逻辑) ---
        if callback_query:
            data = callback_query.get("data")
            message_id = callback_query["message"]["message_id"]
            
            # 响应回调查询，消除加载状态
            await httpx.AsyncClient().post(
                TELEGRAM_API.format(token) + "answerCallbackQuery",
                json={"callback_query_id": callback_query["id"]}
            )

            if data == "cmd_menu":
                # 用户点击了“菜单按钮”
                keyboard = [
                    [{"text": "🔍 立即扫描", "callback_data": "action_scan"}],
                    [{"text": "📊 查看状态", "callback_data": "action_status"}],
                    [{"text": "❓ 帮助", "callback_data": "action_help"}]
                ]
                reply_markup = {"inline_keyboard": keyboard}
                
                await edit_telegram_message(token, chat_id, message_id, 
                    "👋 *欢迎使用空投雷达！*\n点击下方按钮开始操作：", 
                    reply_markup
                )
                return

            elif data == "action_scan":
                # 用户点击了“立即扫描”
                keyboard = [[{"text": "⏳ 扫描中...", "callback_data": "noop"}]]
                reply_markup = {"inline_keyboard": keyboard}
                await edit_telegram_message(token, chat_id, message_id, "⏳ *正在扫描空投，请稍候...*", reply_markup)
                
                # 执行扫描
                scan_count, last_scan_result = await run_radar_and_push()
                
                # 更新面板
                keyboard = [
                    [{"text": "🔍 再次扫描", "callback_data": "action_scan"}],
                    [{"text": "📊 查看状态", "callback_data": "action_status"}]
                ]
                reply_markup = {"inline_keyboard": keyboard}
                await edit_telegram_message(token, chat_id, message_id, 
                    f"✅ *扫描完成 | 第 {scan_count} 次*\n\n"
                    f"🔍 扫描项目: `34` 个\n"
                    f"📡 推送信号: `{last_scan_result.split(',')[1].strip() if ',' in last_scan_result else '0'}` 条\n"
                    f"⏰ 时间: `{datetime.now().strftime('%H:%M:%S')}`\n\n"
                    f"━━━━━━━━━━━━━\n"
                    f"🔴 系统运行中... 等待下次自动扫描", 
                    reply_markup
                )
                return

            elif data == "action_status":
                # 查看状态
                status_text = (
                    f"📊 *系统状态*\n\n"
                    f"扫描次数: `{scan_count}`\n"
                    f"最近结果: `{last_scan_result}`\n"
                    f"运行状态: ✅ 正常"
                )
                await edit_telegram_message(token, chat_id, message_id, status_text, None)
                return

        # --- 2. 处理文本命令 ---
        if message:
            text = message.get("text", "").strip()
            if text == "/start" or text == "/menu":
                # 【关键设计】：这里不再发普通文字，而是直接发一个带按钮的包
                # 这个按钮会显示在输入框左边
                keyboard = [[{"text": "☰ 菜单", "callback_data": "cmd_menu"}]]
                reply_markup = {"inline_keyboard": keyboard}
                
                await send_telegram(token, chat_id, 
                    "👋 空投雷达已激活！点击下方菜单开始：", 
                    reply_markup
                )

    except Exception as e:
        logger.error(f"处理消息出错: {e}")

# ===== FastAPI 生命周期 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 服务启动中...")
    # 上线通知
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        await send_telegram(token, chat_id, 
            f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    asyncio.create_task(handle_telegram_command(body))
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
