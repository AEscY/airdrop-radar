# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("AirdropRadar")

# ===== 配置 =====
ALPHA_DROPS_ALPHA = "https://alphadrops.net/alpha"
ALPHA_DROPS_FREE = "https://alphadrops.net/free-crypto-airdrops"
ALPHA_DROPS_CLAIM = "https://alphadrops.net/claim"

WATCH_PROJECTS = [
    "Infinex", "Based", "RateX", "Pharos Network", "ETHGAS", "Lighter",
    "Superform", "Rainbow", "Quote", "Arcus", "Robinhood Perpl", "Ondo Perps",
    "xStocks", "Fomo", "JTX", "Tori Finance", "Bulk", "Polymarket", "Pacifica",
    "Pascal", "N1", "Upshift", "Cambria", "Backpack", "Monad", "Berachain",
    "Perpl", "Orbinum", "Canopy", "Hypertrade", "Velvet", "Plume", "Grass",
    "MetaMask", "Titan", "Phoenix", "Synq", "Meridian", "Nado", "StandX",
    "Liquid", "Extended", "Hylo"
]

# ===== 全局状态 =====
scan_count = 0
last_scan_result = "从未执行"
last_snapshot = {}  # 用于去重

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
    big_vc = ["Polymarket", "Fomo", "JTX", "Phoenix", "xStocks", "Ondo Perps", "Quote", "Pascal"]
    if name in big_vc:
        s += 8
    return int(s)

# ===== 爬取 Alpha Drops 免费页 =====
async def fetch_alpha_drops():
    drops = []
    html_pages = []
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for url in [ALPHA_DROPS_ALPHA, ALPHA_DROPS_FREE, ALPHA_DROPS_CLAIM]:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 3000:
                    html_pages.append(resp.text)
                    logger.info(f"✅ 抓取成功: {url} ({len(resp.text)} bytes)")
            except Exception as e:
                logger.error(f"❌ 抓取失败 {url}: {e}")
                continue
    if not html_pages:
        return []
    combined_html = "\n".join(html_pages)
    for name in WATCH_PROJECTS:
        idx = combined_html.find(name)
        if idx == -1:
            continue
        ctx_start = max(0, idx - 400)
        ctx_end = min(len(combined_html), idx + 1000)
        ctx = combined_html[ctx_start:ctx_end]
        funding_match = re.search(r"\$([\d.]+)\s*([MB])", ctx)
        funding = ""
        if funding_match:
            funding = f"${funding_match.group(1)}{funding_match.group(2)}"
        chains = []
        for chain in ["Solana", "Ethereum", "Base", "Arbitrum", "Polygon",
                       "Abstract", "Monad", "BNB", "Hyperliquid", "Ink", "Sui", "EVM", "SVM"]:
            if chain.lower() in ctx.lower():
                chains.append(chain)
        chains = list(dict.fromkeys(chains))
        is_claimable = ("claimable" in ctx.lower()) or ("claim" in ctx.lower() and "claim details" in ctx.lower())
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

# ===== Telegram 消息函数 =====
def tg_send_url(token):
    return f"https://api.telegram.org/bot{token}/sendMessage"

def tg_edit_url(token):
    return f"https://api.telegram.org/bot{token}/editMessageText"

def tg_answer_cb_url(token):
    return f"https://api.telegram.org/bot{token}/answerCallbackQuery"

def tg_set_webhook_url(token):
    return f"https://api.telegram.org/bot{token}/setWebhook"

async def send_telegram(token, chat_id, text, reply_markup=None):
    if not token or not chat_id:
        logger.error("❌ 缺少 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await client.post(tg_send_url(token), json=payload)
            if resp.status_code == 200:
                logger.info(f"✅ 推送成功 ({len(text)} chars)")
                return True
            else:
                logger.error(f"❌ 推送失败: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"❌ 发送异常: {e}")
        return False

async def edit_telegram(token, chat_id, message_id, text, reply_markup=None):
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await client.post(tg_edit_url(token), json=payload)
            if resp.status_code == 200:
                return True
            else:
                logger.error(f"❌ 编辑失败: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"❌ 编辑异常: {e}")
        return False

# ===== 扫描并推送 =====
async def run_radar_and_push():
    global scan_count, last_scan_result, last_snapshot
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    scan_count += 1
    logger.info(f"🔄 === 第 {scan_count} 次扫描开始 ===")
    drops = await fetch_alpha_drops()
    if not drops:
        last_scan_result = f"第{scan_count}次: 抓取0个项目"
        logger.warning("⚠️ 未抓到任何项目")
        return 0
    # 去重：只对状态变化或新出现的高分项目推送
    new_signals = []
    for d in drops:
        key = d["name"]
        prev = last_snapshot.get(key, {})
        changed = (prev.get("is_claimable") != d["is_claimable"] or
                   prev.get("score", 0) != d["score"])
        if (changed and d["score"] >= 70) or (d["is_claimable"] and d["score"] >= 60):
            new_signals.append(d)
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    pushed = 0
    for d in new_signals[:5]:
        emoji = "🚨" if d["score"] >= 80 else "⚠️"
        # 转义 Markdown 特殊字符，避免 400 错误
        name_esc = d["name"].replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
        msg = f"{emoji} *[{d['score']}分] {name_esc}*\n"
        msg += f"链: {', '.join(d['blockchains']) or 'N/A'}\n"
        msg += f"融资: {d['funding_amount'] or 'N/A'}\n"
        msg += f"状态: {'✅ 可领取' if d['is_claimable'] else '活跃'}"
        ok = await send_telegram(token, chat_id, msg)
        if ok:
            pushed += 1
        await asyncio.sleep(1)
    # 更新快照
    snapshot = {d["name"]: {"is_claimable": d["is_claimable"], "score": d["score"]} for d in drops}
    last_snapshot = snapshot
    last_scan_result = f"第{scan_count}次: 扫描{len(drops)}个, 推送{pushed}条"
    logger.info(f"✅ === 扫描完成: {last_scan_result} ===")
    return len(drops)

# ===== 构建面板文本 =====
def build_panel(total=None):
    global scan_count, last_scan_result
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t = total if total is not None else 0
    panel = f"📊 *扫描完成 | 第 {scan_count} 次*\n\n"
    panel += f"🔍 扫描项目: `{t}` 个\n"
    panel += f"📡 最近结果: `{last_scan_result}`\n"
    panel += f"⏰ 时间: `{now}`\n\n"
    panel += "━━━━━━━━━━━━━\n"
    panel += "🔴 系统运行中... 每15分钟自动扫描"
    return panel

# ===== 处理 Telegram 更新 =====
async def handle_telegram_update(body):
    global scan_count, last_scan_result
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        return

    # 处理按钮回调
    callback_query = body.get("callback_query")
    if callback_query:
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query.get("data", "")
        # 响应回调，消除加载状态
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(tg_answer_cb_url(token), json={"callback_query_id": callback_query["id"]})
        except Exception:
            pass

        if data == "cmd_menu":
            keyboard = {"inline_keyboard": [
                [{"text": "🔍 立即扫描", "callback_data": "action_scan"}],
                [{"text": "📊 查看状态", "callback_data": "action_panel"}],
                [{"text": "❓ 帮助", "callback_data": "action_help"}]
            ]}
            await edit_telegram(token, chat_id, message_id,
                "☰ *空投雷达控制台*\n\n请选择操作：", keyboard)

        elif data == "action_scan":
            # 先显示扫描中
            keyboard = {"inline_keyboard": [[{"text": "⏳ 扫描中...", "callback_data": "noop"}]]}
            await edit_telegram(token, chat_id, message_id, "⏳ *正在扫描空投，请稍候...*", keyboard)
            # 执行扫描
            total = await run_radar_and_push()
            # 更新为面板
            keyboard = {"inline_keyboard": [
                [{"text": "🔍 再次扫描", "callback_data": "action_scan"}],
                [{"text": "📊 查看面板", "callback_data": "action_panel"}]
            ]}
            await edit_telegram(token, chat_id, message_id, build_panel(total), keyboard)

        elif data == "action_panel":
            keyboard = {"inline_keyboard": [
                [{"text": "🔍 立即扫描", "callback_data": "action_scan"}],
                [{"text": "📊 刷新面板", "callback_data": "action_panel"}]
            ]}
            await edit_telegram(token, chat_id, message_id, build_panel(), keyboard)

        elif data == "action_help":
            help_text = (
                "📖 *帮助*\n\n"
                "*☰ 菜单* — 常驻入口，点击展开控制台\n"
                "*立即扫描* — 手动触发一次爬取\n"
                "*查看面板* — 显示扫描统计\n\n"
                "系统每15分钟自动扫描 Alpha Drops 免费页，"
                "发现高分或可领取空投会自动推送。"
            )
            await edit_telegram(token, chat_id, message_id, help_text, None)
        return

    # 处理文本命令
    message = body.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start" or text == "/menu":
        # 发送带常驻菜单按钮的消息
        keyboard = {"inline_keyboard": [[{"text": "☰ 菜单", "callback_data": "cmd_menu"}]]}
        await send_telegram(token, chat_id,
            "👋 *空投雷达已激活！*\n\n点击下方 ☰ 菜单 打开控制台：", keyboard)
    elif text == "/scan":
        await send_telegram(token, chat_id, "⏳ 正在扫描...")
        total = await run_radar_and_push()
        await send_telegram(token, chat_id, f"✅ 扫描完成！共检查 {total} 个项目。")
    elif text == "/status":
        await send_telegram(token, chat_id, build_panel())
    elif text == "/help":
        await send_telegram(token, chat_id,
            "📖 *帮助*\n/menu — 显示菜单\n/scan — 立即扫描\n/status — 查看状态\n/help — 帮助")

# ===== 生命周期管理 =====
@asynccontextmanager
async def lifespan(app):
    logger.info("🚀 ===== Airdrop Radar 启动中 =====")
    token = os.environ.get("TG_BOT_TOKEN")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    # 设置 Webhook
    if token and render_url:
        webhook_url = f"{render_url}/webhook"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(tg_set_webhook_url(token), json={"url": webhook_url})
                if resp.status_code == 200:
                    logger.info(f"✅ Webhook 设置成功: {webhook_url}")
                else:
                    logger.error(f"❌ Webhook 设置失败: {resp.text}")
        except Exception as e:
            logger.error(f"❌ Webhook 设置异常: {e}")

    # 上线通知
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        await send_telegram(token, chat_id,
            f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n每15分钟自动扫描一次。\n\n发送 /menu 打开控制台。")

    # 定时扫描
    async def periodic_scan():
        while True:
            try:
                await run_radar_and_push()
            except Exception as e:
                logger.error(f"❌ 定时扫描异常: {e}")
            await asyncio.sleep(900)

    task = asyncio.create_task(periodic_scan())
    logger.info("⏰ 定时扫描任务已启动 (每15分钟)")
    yield
    task.cancel()
    logger.info("🛑 服务关闭")

app = FastAPI(lifespan=lifespan)

# ===== Webhook 端点 =====
@app.post("/webhook")
async def telegram_webhook(request):
    body = await request.json()
    asyncio.create_task(handle_telegram_update(body))
    return {"ok": True}

# ===== 其他端点 =====
@app.get("/health")
async def health():
    return {"status": "alive", "scans": scan_count, "last": last_scan_result}

@app.get("/")
async def root():
    return {"service": "Airdrop Radar + Bot", "status": "running", "scans": scan_count}

@app.post("/collect")
async def collect(request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    total = await run_radar_and_push()
    return {"result": f"OK: 扫描{total}个项目, {last_scan_result}"}

@app.get("/run")
async def manual_run():
    total = await run_radar_and_push()
    return {"result": f"OK: 扫描{total}个项目, {last_scan_result}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
