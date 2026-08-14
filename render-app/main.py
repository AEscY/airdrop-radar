# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, List, Dict

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("AirdropRadar")

# ===== 配置 =====
ALPHA_DROPS_FREE = "https://alphadrops.net/free-crypto-airdrops"
ALPHA_DROPS_BEST = "https://alphadrops.net/best-crypto-airdrops-2026"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_EDIT = "https://api.telegram.org/bot{token}/editMessageText"
TELEGRAM_SET_WEBHOOK = "https://api.telegram.org/bot{token}/setWebhook"

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

# ===== 全局状态 =====
scan_count = 0
last_scan_result = "从未执行"

# ===== 评分引擎 =====
def score_drop(name: str, funding_str: str, chains: list, is_claimable: bool, is_premium: bool) -> int:
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
async def fetch_alpha_drops() -> List[Dict]:
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

# ===== Telegram 消息函数 =====
async def send_telegram(token: str, chat_id: str, text: str, reply_markup=None) -> bool:
    if not token or not chat_id:
        logger.error("❌ 缺少 TG_BOT_TOKEN 或 TG_CHAT_ID")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await client.post(TELEGRAM_SEND.format(token=token), json=payload)
            if resp.status_code == 200:
                logger.info(f"✅ Telegram 发送成功 ({len(text)} chars)")
                return True
            else:
                logger.error(f"❌ Telegram 发送失败: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Telegram 发送异常: {e}")
        return False

async def edit_telegram(token: str, chat_id: str, message_id: int, text: str, reply_markup=None) -> bool:
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = await client.post(TELEGRAM_EDIT.format(token=token), json=payload)
            if resp.status_code == 200:
                logger.info(f"✅ Telegram 编辑成功")
                return True
            else:
                logger.error(f"❌ Telegram 编辑失败: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Telegram 编辑异常: {e}")
        return False

# ===== 扫描并推送 =====
async def run_radar_and_push() -> tuple:
    global scan_count, last_scan_result
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    scan_count += 1
    logger.info(f"🔄 === 第 {scan_count} 次扫描开始 ===")
    drops = await fetch_alpha_drops()
    if not drops:
        last_scan_result = f"第{scan_count}次: 抓取0个项目"
        logger.warning("⚠️ 未抓到任何项目")
        return 0, last_scan_result
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
    return len(drops), last_scan_result

# ===== 处理 Telegram 消息 =====
async def handle_telegram_update(body: dict):
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        return

    # 处理 callback_query (按钮点击)
    callback_query = body.get("callback_query")
    if callback_query:
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        data = callback_query.get("data", "")

        if data == "scan_now":
            # 更新消息为“正在扫描”
            await edit_telegram(token, chat_id, message_id, "⏳ *正在扫描空投...*")

            # 执行扫描
            total, result = await run_radar_and_push()

            # 构建控制台面板
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            panel = (
                f"📊 *扫描完成 | 第 {scan_count} 次*\n\n"
                f"🔍 扫描项目: `{total}` 个\n"
                f"📡 推送信号: `{result.split('推送')[1].strip() if '推送' in result else '0'}条`\n"
                f"⏰ 时间: `{now}`\n\n"
                f"━━━━━━━━━━━━━\n"
                f"🔴 系统运行中... 等待下次自动扫描"
            )
            keyboard = {"inline_keyboard": [[{"text": "🔍 再次扫描", "callback_data": "scan_now"}]]}
            await edit_telegram(token, chat_id, message_id, panel, keyboard)
        return

    # 处理文本命令
    message = body.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start" or text == "/menu":
        keyboard = {"inline_keyboard": [[{"text": "🔍 立即扫描", "callback_data": "scan_now"}]]}
        welcome = (
            "👋 *欢迎使用空投雷达！*\n\n"
            "我将每15分钟自动扫描高价值空投。\n"
            "点击下方按钮立即扫描："
        )
        await send_telegram(token, chat_id, welcome, keyboard)

    elif text == "/scan":
        await send_telegram(token, chat_id, "⏳ 正在扫描...")
        total, result = await run_radar_and_push()
        await send_telegram(token, chat_id, f"✅ 扫描完成！共检查 {total} 个项目。\n{result}")

    elif text == "/status":
        msg = f"📊 *系统状态*\n\n扫描次数: {scan_count}\n最近结果: {last_scan_result}\n运行状态: ✅ 正常"
        await send_telegram(token, chat_id, msg)

    elif text == "/help":
        help_msg = (
            "📖 *帮助*\n\n"
            "/menu - 显示菜单\n"
            "/scan - 立即扫描\n"
            "/status - 查看状态\n"
            "/help - 帮助\n\n"
            "系统每15分钟自动扫描一次。"
        )
        await send_telegram(token, chat_id, help_msg)

# ===== 生命周期管理 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 ===== Airdrop Radar 启动中 =====")
    token = os.environ.get("TG_BOT_TOKEN")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    # 设置 Webhook
    if token and render_url:
        webhook_url = f"{render_url}/webhook"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(TELEGRAM_SET_WEBHOOK.format(token=token), json={"url": webhook_url})
            if resp.status_code == 200:
                logger.info(f"✅ Webhook 设置成功: {webhook_url}")
            else:
                logger.error(f"❌ Webhook 设置失败: {resp.text}")

    # 发送上线通知
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        await send_telegram(token, chat_id,
            f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n每15分钟自动扫描一次。")

    # 启动定时扫描
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
async def telegram_webhook(request: Request):
    body = await request.json()
    asyncio.create_task(handle_telegram_update(body))
    return {"ok": True}

# ===== 其他端点 =====
@app.get("/health")
async def health():
    return {"status": "alive", "scans": scan_count, "last": last_scan_result}

@app.get("/")
async def root():
    return {"service": "Airdrop Radar + Bot", "status": "running", "scans": scan_count, "last_scan": last_scan_result}

@app.post("/collect")
async def collect(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    total, result = await run_radar_and_push()
    return {"result": f"OK: 扫描{total}个, {result}"}

@app.get("/run")
async def manual_run():
    total, result = await run_radar_and_push()
    return {"result": f"OK: 扫描{total}个, {result}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
