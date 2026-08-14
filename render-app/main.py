# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
import time
from datetime import datetime
from threading import Thread, Event

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== 日志配置 =====
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("AirdropRadar")

# ===== 配置 =====
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
    "Pascal", "Titan", "Phoenix", "Extended", "Hylo",
    "Nado", "Synq", "StandX", "Liquid", "Meridian"
]

# ===== 全局状态 =====
bot_app = None
bot_start_time = None
last_bot_error = None
scan_count = 0
last_scan_result = "从未执行"

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
    big_vc = ["Polymarket", "Fomo", "JTX", "Phoenix", "xStocks", "Ondo Perps", "Quote"]
    if name in big_vc:
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
                    logger.info(f"✅ 抓取成功: {url} ({len(resp.text)} bytes)")
                else:
                    logger.warning(f"⚠️ 抓取返回 {resp.status_code}: {url}")
            except Exception as e:
                logger.error(f"❌ 抓取失败 {url}: {e}")
                continue
    if not html_pages:
        logger.error("❌ 所有页面抓取均失败")
        return []
    combined_html = "\n".join(html_pages)
    logger.info(f"📄 合并 HTML 总长度: {len(combined_html)}")
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
        for chain in ["Solana", "Ethereum", "Base", "Arbitrum", "Polygon",
                       "Abstract", "Monad", "BNB", "Hyperliquid", "Ink", "Sui"]:
            if chain.lower() in ctx.lower():
                chains.append(chain)
        chains = list(dict.fromkeys(chains))
        is_claimable = "claimable" in ctx.lower()
        is_premium = "premium" in ctx.lower()
        sc = score_drop(name, funding, chains, is_claimable, is_premium)
        drops.append({
            "name": name, "funding_amount": funding,
            "blockchains": chains, "is_claimable": is_claimable,
            "premium": is_premium, "score": sc
        })
        logger.info(f"  📌 {name} | 分数:{sc} | 可领取:{is_claimable} | 链:{chains}")
    seen = {}
    for d in drops:
        if d["name"] not in seen:
            seen[d["name"]] = d
    unique = list(seen.values())
    logger.info(f"📊 去重后共 {len(unique)} 个项目")
    return unique

# ===== 发送 Telegram 消息 =====
async def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        logger.error("❌ 缺少 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                TELEGRAM_SEND.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            if resp.status_code == 200:
                logger.info(f"✅ Telegram 推送成功 ({len(text)} chars)")
                return True
            else:
                logger.error(f"❌ Telegram 推送失败: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Telegram 发送异常: {e}")
        return False

# ===== 扫描并推送 =====
async def run_radar_and_push():
    global scan_count, last_scan_result
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    scan_count += 1
    logger.info(f"🔄 === 第 {scan_count} 次扫描开始 ===")
    drops = await fetch_alpha_drops()
    if not drops:
        last_scan_result = f"第{scan_count}次: 抓取0个项目"
        logger.warning("⚠️ 未抓到任何项目")
        return 0
    new_signals = [d for d in drops if d["score"] >= 75 or d["is_claimable"]]
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    pushed = 0
    for d in new_signals[:5]:
        emoji = "🚨" if d["score"] >= 90 else "⚠️" if d["score"] >= 80 else "📊"
        msg = f"{emoji} [{d['score']}分] *{d['name']}*\n"
        msg += f"链: {', '.join(d['blockchains']) or 'N/A'}\n"
        msg += f"融资: {d['funding_amount'] or 'N/A'}\n"
        msg += f"状态: {'✅ 可领取' if d['is_claimable'] else '活跃'}"
        ok = await send_telegram(token, chat_id, msg)
        if ok:
            pushed += 1
        await asyncio.sleep(1)
    last_scan_result = f"第{scan_count}次: 扫描{len(drops)}个, 推送{pushed}条"
    logger.info(f"✅ === 扫描完成: {last_scan_result} ===")
    return pushed

# ===== Telegram Bot 命令 =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 空投雷达已连接！发送 /menu 查看菜单。")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 立即扫描", callback_data='scan')],
        [InlineKeyboardButton("📊 查看状态", callback_data='status')],
        [InlineKeyboardButton("📋 项目列表", callback_data='list')],
        [InlineKeyboardButton("ℹ️ 帮助", callback_data='help')],
    ]
    await update.message.reply_text("请选择操作：", reply_markup=InlineKeyboardMarkup(keyboard))

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在扫描，请稍候...")
    pushed = await run_radar_and_push()
    await update.message.reply_text(f"✅ 扫描完成！推送了 {pushed} 条信号。")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = "未知"
    if bot_start_time:
        seconds = int(time.time() - bot_start_time)
        uptime = f"{seconds//60}分{seconds%60}秒"
    msg = f"📊 *系统状态*\n"
    msg += f"运行时间: {uptime}\n"
    msg += f"扫描次数: {scan_count}\n"
    msg += f"最近结果: {last_scan_result}\n"
    msg += f"Bot状态: {'✅ 正常' if bot_app else '❌ 离线'}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 正在获取项目列表...")
    drops = await fetch_alpha_drops()
    if not drops:
        await update.message.reply_text("😢 暂时无法获取数据。")
        return
    drops.sort(key=lambda x: x["score"], reverse=True)
    msg = "📋 *监控项目 TOP 10*\n\n"
    for d in drops[:10]:
        emoji = "🚨" if d["score"] >= 80 else "📊"
        msg += f"{emoji} *{d['name']}* — {d['score']}分\n"
        msg += f"  链: {', '.join(d['blockchains'][:2]) or 'N/A'}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "📖 *帮助*\n\n"
    msg += "/menu — 显示菜单\n"
    msg += "/scan — 立即扫描空投\n"
    msg += "/status — 查看系统状态\n"
    msg += "/list — 查看监控项目列表\n"
    msg += "/help — 显示此帮助\n\n"
    msg += "系统每15分钟自动扫描一次，发现高分空投会自动推送。"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'scan':
        await query.edit_message_text("⏳ 正在扫描...")
        pushed = await run_radar_and_push()
        await query.edit_message_text(f"✅ 扫描完成！推送了 {pushed} 条信号。")
    elif data == 'status':
        uptime = "未知"
        if bot_start_time:
            seconds = int(time.time() - bot_start_time)
            uptime = f"{seconds//60}分{seconds%60}秒"
        msg = f"📊 运行时间: {uptime}\n扫描次数: {scan_count}\n最近: {last_scan_result}"
        await query.edit_message_text(msg)
    elif data == 'list':
        await query.edit_message_text("⏳ 获取中...")
        drops = await fetch_alpha_drops()
        drops.sort(key=lambda x: x["score"], reverse=True)
        msg = "📋 *TOP 10*\n\n"
        for d in drops[:10]:
            msg += f"• *{d['name']}* — {d['score']}分\n"
        await query.edit_message_text(msg, parse_mode="Markdown")
    elif data == 'help':
        await query.edit_message_text("发送 /help 查看完整帮助。")

# ===== Bot 启动（带重试）=====
def start_bot_with_retry():
    global bot_app, bot_start_time, last_bot_error
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token:
        logger.error("❌ 未设置 TG_BOT_TOKEN，Bot 无法启动")
        return
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🤖 Bot 启动尝试 {attempt}/{max_retries}...")
            bot_app = Application.builder().token(token).build()
            bot_app.add_handler(CommandHandler("start", start_cmd))
            bot_app.add_handler(CommandHandler("menu", menu_cmd))
            bot_app.add_handler(CommandHandler("scan", scan_cmd))
            bot_app.add_handler(CommandHandler("status", status_cmd))
            bot_app.add_handler(CommandHandler("list", list_cmd))
            bot_app.add_handler(CommandHandler("help", help_cmd))
            bot_app.add_handler(CallbackQueryHandler(button_handler))
            bot_start_time = time.time()
            logger.info("✅ Bot 初始化成功，开始 Polling...")
            # 发送上线通知
            if chat_id:
                asyncio.run(send_telegram(token, chat_id,
                    f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n发送 /menu 开始使用。"))
            bot_app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            # 如果 run_polling 返回了，说明 Bot 停止了
            logger.warning("⚠️ Bot Polling 已停止，准备重启...")
            last_bot_error = "Polling stopped"
        except Exception as e:
            last_bot_error = str(e)
            logger.error(f"❌ Bot 异常 (第{attempt}次): {e}")
            wait = min(2 ** attempt, 60)  # 指数退避，最多等60秒
            logger.info(f"⏳ {wait}秒后重试...")
            time.sleep(wait)
    logger.error("❌❌ Bot 达到最大重试次数，彻底停止")

# ===== 定时扫描 =====
async def periodic_scan():
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        logger.error("❌ 缺少 Token 或 Chat ID，定时扫描停止")
        return
    logger.info("⏰ 定时扫描任务启动，每15分钟执行一次")
    while True:
        try:
            await run_radar_and_push()
        except Exception as e:
            logger.error(f"❌ 定时扫描异常: {e}")
        logger.info("💤 休眠15分钟...")
        await asyncio.sleep(900)

# ===== FastAPI =====
from fastapi import FastAPI, Request
app = FastAPI()

@app.on_event("startup")
async def startup():
    logger.info("🚀 ===== Airdrop Radar 启动中 =====")
    # 启动 Bot 线程
    t = Thread(target=start_bot_with_retry, daemon=True)
    t.start()
    logger.info("🧵 Bot 线程已启动")
    # 启动定时扫描
    asyncio.create_task(periodic_scan())
    logger.info("⏰ 定时扫描任务已创建")
    # 发上线通知
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        await send_telegram(token, chat_id,
            f"🟢 *服务已启动*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n发送 /menu 查看菜单。")

@app.get("/health")
async def health():
    return {"status": "alive", "scans": scan_count, "last": last_scan_result}

@app.get("/")
async def root():
    return {
        "service": "Airdrop Radar + Bot",
        "status": "running",
        "bot": "alive" if bot_app else "dead",
        "scans": scan_count,
        "last_scan": last_scan_result
    }

@app.post("/collect")
async def collect(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    pushed = await run_radar_and_push()
    return {"result": f"OK: 推送 {pushed} 条信号"}

@app.get("/run")
async def manual_run():
    pushed = await run_radar_and_push()
    return {"result": f"OK: 推送 {pushed} 条信号"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
