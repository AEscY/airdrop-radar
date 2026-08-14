# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
logger = logging.getLogger("AirdropRadar")

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

scan_count = 0
last_scan_result = "从未执行"

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

async def fetch_alpha_drops():
    drops = []
    html_pages = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for url in [ALPHA_DROPS_FREE, ALPHA_DROPS_BEST]:
            try:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 ===== Airdrop Radar 启动中 =====")
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if token and chat_id:
        await send_telegram(token, chat_id,
            f"🟢 *空投雷达已上线！*\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n每15分钟自动扫描一次。")
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

@app.get("/health")
async def health():
    return {"status": "alive", "scans": scan_count, "last": last_scan_result}

@app.get("/")
async def root():
    return {"service": "Airdrop Radar", "status": "running", "scans": scan_count, "last_scan": last_scan_result}

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
