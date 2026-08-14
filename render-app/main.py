import os
import asyncio
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

# 初始化 FastAPI
app = FastAPI()

# --- 全局状态 ---
scan_count = 0
last_scan_result = "尚未扫描"

# --- 常量 ---
TELEGRAM_SET_WEBHOOK = "https://api.telegram.org/bot{token}/setWebhook"
TELEGRAM_SEND_MESSAGE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_EDIT_MESSAGE = "https://api.telegram.org/bot{token}/editMessageText"

# --- 模拟扫描逻辑 ---
async def run_radar_and_push():
    global scan_count, last_scan_result
    scan_count += 1
    
    # 模拟网络请求耗时
    await asyncio.sleep(2)
    
    # 模拟扫描结果
    total_projects = 34 + scan_count
    new_signals = 0 # 假设没有新信号
    
    result_text = f"扫描 {total_projects} 个项目, 发现 {new_signals} 个新机会"
    last_scan_result = result_text
    
    # 如果有 Chat ID，实际发送推送
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        try:
            await send_telegram(token, chat_id, f"🔄 扫描完成！\n共检查 {total_projects} 个项目，发现 {new_signals} 个新机会。")
        except Exception as e:
            print(f"推送失败: {e}")
            
    return total_projects, new_signals

# --- Telegram API 封装 ---
async def send_telegram(token: str, chat_id: str, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    async with httpx.AsyncClient(timeout=10)0) as client:
        # 【修复点】：确保 URL 中没有多余的 {} 导致 format 报错
        url = TELEGRAM_SEND_MESSAGE.format(token=token)
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

async def edit_telegram_message(token: str, chat_id: str, message_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    async with httpx.AsyncClient(timeout=10)0) as client:
        # 【修复点】：同上，确保 URL 安全
        url = TELEGRAM_EDIT_MESSAGE.format(token=token)
        resp = await client.post(url, json=payload)
        return resp.json()

# --- 命令处理器 ---
async def handle_telegram_command(body: dict):
    global scan_count, last_scan_result
    
    try:
        message = body.get("message")
        callback_query = body.get("callback_query")
        
        # 1. 处理按钮点击 (Callback Query)
        if callback_query:
            chat_id = callback_query["message"]["chat"]["id"]
            message_id = callback_query["message"]["message_id"]
            data = callback_query["data"]
            token = os.environ.get("TG_BOT_TOKEN")
            
            if not token: return

            if data == "cmd_menu":
                # 点击 "☰ 菜单" 按钮，展示主面板
                keyboard = [[{"text": "🔍 立即扫描", "callback_data": "action_scan"}]]
                reply_markup = {"inline_keyboard": keyboard}
                
                welcome_text = (
                    "👋 *空投雷达控制台*\n\n"
                    "点击下方按钮开始扫描高价值空投。"
                )
                await edit_telegram_message(token, chat_id, message_id, welcome_text, reply_markup)
                return

            elif data == "action_scan":
                # 点击 "立即扫描"
                keyboard = [[{"text": "🔄 扫描中...", "callback_data": "action_scan"}]]
                reply_markup = {"inline_keyboard": keyboard}
                
                # 1. 更新为加载状态
                await edit_telegram_message(token, chat_id, message_id, "⏳ *正在扫描空投，请稍候...*", reply_markup)
                
                # 2. 执行扫描
                total, result = await run_radar_and_push()
                
                # 3. 更新为结果面板
                panel_text = (
                    f"*扫描完成 | 第 {scan_count} 次*

"
                    f"🔍 扫描项目: `{total}` 个
"
                    f"📡 推送信号: `{result.split(',')[1].strip() if ',' in result else '0'}` 条
"
                    f"⏰ 时间: `{datetime.now().strftime('%H:%M:%S')}`

"
                    f"━━━━━━━━━━━━━
"
                    f"🔴 系统运行中... 等待下次自动扫描"
                )
                # 重置按钮
                keyboard = [[{"text": "🔍 再次扫描", "callback_data": "action_scan"}]]
                reply_markup = {"inline_keyboard": keyboard}
                
                await edit_telegram_message(token, chat_id, message_id, panel_text, reply_markup)
                return

            elif data == "action_status":
                # 查看状态
                status_text = (
                    f"📊 *系统状态*

"
                    f"扫描次数: `{scan_count}`
"
                    f"最近结果: `{last_scan_result}`
"
                    f"运行状态: ✅ 正常"
                )
                await edit_telegram_message(token, chat_id, message_id, status_text, None)
                return

        # 2. 处理文本命令
        if message:
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            token = os.environ.get("TG_BOT_TOKEN")
            
            if not token: return

            if text == "/start" or text == "/menu":
                # 【关键设计】：发送带有 "☰ 菜单" 按钮的消息
                keyboard = [[{"text": "☰ 菜单", "callback_data": "cmd_menu"}]]
                reply_markup = {"inline_keyboard": keyboard}
                
                await send_telegram(token, chat_id, 
                    "👋 空投雷达已激活！点击左侧菜单开始：", 
                    reply_markup
                )
                return

    except Exception as e:
        logger.error(f"处理消息出错: {e}")

# ===== FastAPI 生命周期 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 服务启动中...")
    # 设置 Webhook (如果需要)
    token = os.environ.get("TG_BOT_TOKEN")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if token and render_url:
        webhook_url = f"{render_url}/webhook"
        try:
            async with httpx.AsyncClient(timeout=10)0) as client:
                # 【修复点】：确保 URL 安全
                url = TELEGRAM_SET_WEBHOOK.format(token=token)
                resp = await client.post(url, json={"url": webhook_url})
                if resp.status_code == 200:
                    print(f"✅ Webhook 设置成功: {webhook_url}")
                else:
                    print(f"❌ Webhook 设置失败: {resp.text}")
        except Exception as e:
            print(f"Webhook 设置异常: {e}")
            
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    asyncio.create_task(handle_telegram_command(body))
    return {"ok": True}

# ===== 本地调试入口 =====
if __name__ == "__main__":
    import uvicorn
    # 本地运行时需要设置环境变量，或者你可以直接在代码里填测试值
    if not os.environ.get("TG_BOT_TOKEN"):
        print("⚠️ 警告: 未设置 TG_BOT_TOKEN 环境变量")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
