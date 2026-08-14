# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
from datetime import datetime
from threading import Thread

from fastapi import FastAPI, Request
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== 配置 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALPHA_DROPS_FREE = "https://alphadrops.net/free-crypto-airdrops"
ALPHA_DROPS_BEST = "https://alphadrops.net/best-crypto-airdrops-2026"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"

WATCH_PROJECTS = [
    "Infinex", "Based", "RateX", "Pharos Network", "ETHGAS", "Lighter",
    "Superform", "Rainbow",
    "Quote", "Arcus", "Robinhood Perpl", "Ondo Perps", "xStocks", "Fomo",
    "JTX", "Tori Finance", "Bulk", "Polymarket", "Pacifica", "QFEX",
    "N1", "Upshift",
    "Cambria", "Backpack", "Monad", "Berachain", "Perpl", "Orbinum",
    "Canopy", "Hypertrade", "Velvet", "Plume", "Grass", "MetaMask",
    "Pascal", "Titan", "Phoenix", "Extended", "Hylo", "RateX",
    "Arcus", "Nado", "Synq", "StandX", "Liquid", "Meridian"
]

# ===== 评分引擎 =====
def score_drop(name, funding_str, chains, is_claimable, is_premium):
    s = 0
    m = re.search(r"\$?([\d.]+)\s*([MB])?", funding_str or "")
    if m:
        amt = float(m.group(1))
        if m.group(2) == "B":
            amt *= 1000
        s += min(amt / 10, 30)
    if is_claimable:
        s += 25
    if "Solana" in (chains or []):
        s += 5
    if is_premium:
        s += 10
    if is_claimable and funding_str:
        s += 5
    big_vc_projects = ["Polymarket", "Fomo", "JTX", "Phoenix", "xStocks", "Ondo Perps", "Quote"]
    if name in big_vc_projects:
        s += 8
    return int(s)

# ===== 爬取 Alpha Drops =====
async def fetch_alpha_drops():
    drops = []
    html_pages = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for url in [ALPHA_DROPS_FREE, ALPHA_DROPS_BEST]:
            try:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
                    }
                )
                if resp.status_code == 200 and len(resp.text) > 3000:
                    html_pages.append(resp.text)
            except Exception:
                continue
    if not html_pages:
        return []
    combined_html = "\n".join(html_pages)
    for name in WATCH_PROJECTS:
        idx = combined_html.find(name)
        if idx == -1:
            continue
        ctx_start = max(0, idx - 300)
        ctx_end = min(len(combined_html), idx + 900)
        ctx = combined_html[ctx_start:ctx_end]
        funding_match = re.search(r"\$([\d.]+)\s*([MB])", ctx)
        funding = ""
        if funding_match:
            funding = f"${funding_match.group(1)}{funding_match.group(2)}"
        chains = []
        chain_keywords = [
            "Solana", "Ethereum", "Base", "Arbitrum", "Polygon",
            "Abstract", "Monad", "BNB", "Hyperliquid", "Ink", "Sui",
            "SVM", "EVM", "L1", "L2"
        ]
        for chain in chain_keywords:
            if chain.lower() in ctx.lower():
                chains.append(chain)
        chains = list(dict.fromkeys(chains))
        is_claimable = "claimable" in ctx.lower()
        is_premium = "premium" in ctx.lower()
        sc = score_drop(name, funding, chains, is_claimable, is_premium)
        drops.append({
            "name": name,
            "funding_amount": funding,
            "blockchains": chains,
            "is_claimable": is_claimable,
            "premium": is_premium,
            "score": sc
        })
    seen = {}
    for d in drops:
        if d["name"] not in seen:
            seen[d["name"]] = d
    return list(seen.values())

# ===== 发送 Telegram 消息（直接 API）=====
async def send_telegram(token, chat_id, text, parse_mode="Markdown"):
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            TELEGRAM_SEND.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )

# ===== 扫描并推送 =====
async def run_radar_and_push(token, chat_id):
    drops = await fetch_alpha_drops()
    # 简单去重：只推送分数>=70或可领取的
    new_signals = [d for d in drops if d["score"] >= 75 or d["is_claimable"]]
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    pushed = 0
    for d in new_signals[:5]:
        emoji = "🚨" if d["score"] >= 90 else "⚠️" if d["score"] >= 80 else "📊"
        msg = f"{emoji} [{d['score']}分] {d['name']}\n"
        msg += f"链: {', '.join(d['blockchains']) or 'N/A'}\n"
        msg += f"融资: {d['funding_amount'] or 'N/A'}\n"
        msg += f"状态: {'✅ 可领取' if d['is_claimable'] else '活跃'}\n"
        if d["premium"]:
            msg += f"级别: Premium\n"
        msg += f"来源: alphadrops.net"
        await send_telegram(token, chat_id, msg)
        pushed += 1
        await asyncio.sleep(0.5)
    return len(drops), pushed

# ===== Telegram Bot 命令处理 =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("欢迎使用空投雷达！发送 /menu 查看选项。")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 立即扫描", callback_data='scan')],
        [InlineKeyboardButton("📊 查看状态", callback_data='status')],
        [InlineKeyboardButton("ℹ️ 帮助", callback_data='help')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("请选择操作：", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'scan':
        await query.edit_message_text("⏳ 正在扫描空投...")
        token = os.environ.get("TG_BOT_TOKEN")
        chat_id = query.message.chat_id
        total, pushed = await run_radar_and_push(token, chat_id)
        await query.edit_message_text(f"✅ 扫描完成！共检查 {total} 个项目，推送 {pushed} 条新信号。")
    elif query.data == 'status':
        await query.edit_message_text("✅ 系统正常运行，每15分钟自动扫描一次。")
    elif query.data == 'help':
        await query.edit_message_text(
            "可用命令：\n"
            "/menu - 显示菜单\n"
            "/scan - 立即扫描\n"
            "/status - 查看状态\n"
            "/help - 帮助"
        )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在扫描空投...")
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = update.message.chat_id
    total, pushed = await run_radar_and_push(token, chat_id)
    await update.message.reply_text(f"✅ 扫描完成！共检查 {total} 个项目，推送 {pushed} 条新信号。")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ 系统正常运行，每15分钟自动扫描一次。")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "可用命令：\n"
        "/menu - 显示菜单\n"
        "/scan - 立即扫描\n"
        "/status - 查看状态\n"
        "/help - 帮助"
    )

# ===== 启动 Telegram Bot（Polling 模式）=====
def start_bot():
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        logger.error("未设置 TG_BOT_TOKEN，机器人无法启动")
        return
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Telegram Bot 启动中...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# ===== 定时扫描任务（每15分钟）=====
async def periodic_scan():
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        logger.warning("未设置 TG_BOT_TOKEN 或 TG_CHAT_ID，定时扫描停止")
        return
    while True:
        logger.info("执行定时扫描...")
        total, pushed = await run_radar_and_push(token, chat_id)
        logger.info(f"定时扫描完成：{total} 个项目，推送 {pushed} 条")
        await asyncio.sleep(900)  # 15分钟

# ===== FastAPI 应用 =====
app = FastAPI()

@app.on_event("startup")
async def startup():
    # 启动 Telegram Bot 线程
    t = Thread(target=start_bot, daemon=True)
    t.start()
    # 启动定时扫描任务
    asyncio.create_task(periodic_scan())

@app.get("/health")
async def health():
    return {"status": "alive"}

@app.post("/collect")
async def collect(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    total, pushed = await run_radar_and_push(token, chat_id)
    return {"result": f"OK: 扫描 {total} 个空投，推送 {pushed} 条新信号"}

@app.get("/run")
async def manual_run():
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    total, pushed = await run_radar_and_push(token, chat_id)
    return {"result": f"OK: 扫描 {total} 个空投，推送 {pushed} 条新信号"}

@app.get("/")
async def root():
    return {"service": "Airdrop Radar + Bot", "status": "running"}
