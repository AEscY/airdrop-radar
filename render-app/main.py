# -*- coding: utf-8 -*-
import os
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("AirdropRadar")

# ===== 全局状态 =====
scan_count = 0
last_scan_result = "从未执行"

# ===== 模拟扫描函数（替换为真实爬虫逻辑） =====
async def run_radar_and_push():
    global scan_count, last_scan_result
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    scan_count += 1
    logger.info(f"🔄 第 {scan_count} 次扫描开始")
    
    # 模拟扫描
    await asyncio.sleep(2)
    total = 42  # 假设扫描了42个项目
    new_signals = 1  # 假设发现1个新信号
    last_scan_result = f"第{scan_count}次: 扫描{total}个, 推送{new_signals}条"
    
    # 发送状态更新
    if token and chat_id:
        msg = (
            f"✅ *扫描完成 | 第 {scan_count} 次*\n\n"
            f"🔍 扫描项目: `{total}` 个\n"
            f"📡 推送信号: `{new_signals}` 条\n"
            f"⏰ 时间: `{datetime.now().strftime('%H:%M:%S')}`\n\n"
            f"━━━━━━━━━━━━━\n"
            f"🔴 系统运行中..."
        )
        await send_telegram(token, chat_id, msg)
    return total

# ===== Telegram API 封装 =====
async def send_telegram(token: str, chat_id: str, text: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        return resp.status_code == 200

async def edit_telegram(token: str, chat_id: str, message_id: int, text: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        return resp.status_code == 200

# ===== 命令处理器 =====
async def handle_update(body: dict):
    global scan_count, last_scan_result
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        return

    # 处理 callback_query
    cb = body.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        data = cb["data"]

        # 应答回调查询，消除加载动画
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                              json={"callback_query_id": cb["id"]})

        if data == "cmd_menu":
            # 点击 ☰ 菜单 → 显示操作面板
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔍 立即扫描", "callback_data": "action_scan"}],
                    [{"text": "📊 查看状态", "callback_data": "action_status"}],
                    [{"text": "❓ 帮助", "callback_data": "action_help"}]
                ]
            }
            await edit_telegram(token, chat_id, msg_id, "☰ *请选择操作：*", keyboard)

        elif data == "action_scan":
            # 点击立即扫描
            await edit_telegram(token, chat_id, msg_id, "⏳ *正在扫描空投，请稍候...*")
            total = await run_radar_and_push()
            # 更新面板
            panel = (
                f"✅ *扫描完成 | 第 {scan_count} 次*\n\n"
                f"🔍 扫描项目: `{total}` 个\n"
                f"📡 最近结果: `{last_scan_result}`\n"
                f"⏰ 时间: `{datetime.now().strftime('%H:%M:%S')}`\n\n"
                f"━━━━━━━━━━━━━\n"
                f"🔴 系统运行中..."
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🔍 再次扫描", "callback_data": "action_scan"}],
                    [{"text": "📊 刷新面板", "callback_data": "action_status"}]
                ]
            }
            await edit_telegram(token, chat_id, msg_id, panel, keyboard)

        elif data == "action_status":
            status = (
                f"📊 *系统状态*\n\n"
                f"扫描次数: `{scan_count}`\n"
                f"最近结果: `{last_scan_result}`\n"
                f"运行状态: ✅ 正常"
            )
            await edit_telegram(token, chat_id, msg_id, status, None)

        elif data == "action_help":
            help_text = (
                "📖 *帮助*\n\n"
                "• 点击 ☰ 菜单 打开控制台\n"
                "• 点击 立即扫描 手动触发扫描\n"
                "• 系统每15分钟自动扫描一次\n"
                "• 发现高分空投自动推送"
            )
            await edit_telegram(token, chat_id, msg_id, help_text, None)
        return

    # 处理文本命令
    msg = body.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if text == "/start" or text == "/menu":
        # 发送一条带“☰ 菜单”按钮的消息，按钮会显示在输入框正上方（ReplyKeyboardMarkup）
        keyboard = {"keyboard": [[{"text": "☰ 菜单"}]], "resize_keyboard": True, "one_time_keyboard": False}
        await send_telegram(token, chat_id,
            "👋 *空投雷达已激活！*\n\n点击下方「☰ 菜单」按钮打开控制台：",
            keyboard)
    elif text == "☰ 菜单":
        # 用户点击了输入框上方的“☰ 菜单”按钮 → 模拟 callback 处理
        # 但由于这是 ReplyKeyboardMarkup 的按钮，我们无法直接 callback，只能当作普通文本处理
        # 所以我们改为发送一条带内联按钮的新消息
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔍 立即扫描", "callback_data": "action_scan"}],
                [{"text": "📊 查看状态", "callback_data": "action_status"}],
                [{"text": "❓ 帮助", "callback_data": "action_help"}]
            ]
        }
        await send_telegram(token, chat_id, "☰ *空投雷达控制台*\n\n请选择操作：", keyboard)
    elif text == "/scan":
        await send_telegram(token, chat_id, "⏳ 正在扫描...")
        total = await run_radar_and_push()
        await send_telegram(token, chat_id, f"✅ 扫描完成！共检查 {total} 个项目。")
    elif text == "/status":
        await send_telegram(token, chat_id,
            f"📊 *状态*\n扫描次数: {scan_count}\n最近结果: {last_scan_result}")
    elif text == "/help":
        await send_telegram(token, chat_id, "📖 帮助: /menu 打开菜单，/scan 立即扫描，/status 查看状态")

# ===== FastAPI 生命周期 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 启动中...")
    token = os.environ.get("TG_BOT_TOKEN")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    chat_id = os.environ.get("TG_CHAT_ID")

    # 设置 Webhook
    if token and render_url:
        webhook_url = f"{render_url}/webhook"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"https://api.telegram.org/bot{token}/setWebhook",
                                     json={"url": webhook_url})
            if resp.status_code == 200:
                logger.info(f"✅ Webhook 设置成功: {webhook_url}")
            else:
                logger.error(f"❌ Webhook 设置失败: {resp.text}")

    # 上线通知
    if token and chat_id:
        await send_telegram(token, chat_id,
            f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n发送 /menu 开始使用。")

    # 定时扫描
    async def periodic_scan():
        while True:
            try:
                await run_radar_and_push()
            except Exception as e:
                logger.error(f"定时扫描异常: {e}")
            await asyncio.sleep(900)
    task = asyncio.create_task(periodic_scan())
    logger.info("⏰ 定时扫描已启动 (每15分钟)")
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    asyncio.create_task(handle_update(body))
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "running", "scans": scan_count, "last": last_scan_result}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
