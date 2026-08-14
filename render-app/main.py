# -*- coding: utf-8 -*-
import os
import re
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request

# ===== 日志 =====
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("AirdropRadar")

# ===== 配置 =====
ALPHA_API = "https://alphadrops.net/api/v1/airdrops"
ALPHA_FREE_PAGE = "https://alphadrops.net/alpha"
TELEGRAM_API = "https://api.telegram.org/bot{token}"

# 监控的项目
WATCH_KEYWORDS = [
    "Infinex", "Based", "RateX", "Pharos", "Quote", "Arcus",
    "Robinhood Perpl", "Ondo Perps", "xStocks", "Fomo", "JTX",
    "Polymarket", "Pascal", "Titan", "Phoenix", "Monad",
    "Berachain", "Grass", "MetaMask", "N1", "QFEX"
]

# ===== 全局状态 =====
scan_count = 0
last_scan_result = "从未执行"
user_wallets = {}          # chat_id -> wallet address
user_settings = {}         # chat_id -> {"min_score": 70, "chains": []}
last_snapshot = {}         # 去重用

# ===== 评分引擎 =====
def score_project(name: str, funding: str, is_claimable: bool, is_premium: bool, chains: list) -> int:
    s = 0
    m = re.search(r"\$?([\d.]+)\s*([MB])?", funding or "")
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
    if is_claimable and funding:
        s += 5
    big_vc = ["Polymarket", "Fomo", "JTX", "Phoenix", "xStocks", "Ondo Perps", "Quote", "Pascal"]
    if name in big_vc:
        s += 8
    return int(s)

# ===== 数据抓取 =====
async def fetch_airdrops() -> list:
    """优先用 API，失败则降级到 HTML 解析"""
    token = os.environ.get("ALPHA_API_KEY")
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
    }

    # 尝试 API
    if token:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    ALPHA_API,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"status": "active", "limit": 50}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        logger.info(f"✅ API 抓取成功: {len(data['data'])} 个项目")
                        return data["data"]
                else:
                    logger.warning(f"⚠️ API 返回 {resp.status_code}，降级到 HTML")
        except Exception as e:
            logger.error(f"❌ API 调用失败: {e}")

    # 降级：HTML 解析
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(ALPHA_FREE_PAGE, headers=headers)
            if resp.status_code != 200:
                logger.error(f"❌ HTML 抓取失败: {resp.status_code}")
                return []
            html = resp.text
    except Exception as e:
        logger.error(f"❌ HTML 抓取异常: {e}")
        return []

    # 解析 HTML 中的项目
    results = []
    for kw in WATCH_KEYWORDS:
        idx = html.find(kw)
        if idx == -1:
            continue
        snippet = html[max(0, idx-200): idx+600]
        funding_m = re.search(r"\$([\d.]+)\s*([MB])", snippet)
        funding = f"${funding_m.group(1)}{funding_m.group(2)}" if funding_m else ""
        chains = []
        for c in ["Ethereum", "Solana", "Base", "Arbitrum", "Polygon", "Monad", "BNB", "Hyperliquid", "Sui", "Ink"]:
            if c.lower() in snippet.lower():
                chains.append(c)
        is_claimable = "claimable" in snippet.lower()
        is_premium = "Premium" in snippet
        results.append({
            "name": kw,
            "funding_amount": funding,
            "blockchains": chains,
            "is_claimable": is_claimable,
            "premium": is_premium
        })

    # 去重
    seen = {}
    for r in results:
        if r["name"] not in seen:
            seen[r["name"]] = r
    logger.info(f"📊 HTML 解析得到 {len(seen)} 个项目")
    return list(seen.values())

# ===== Telegram 封装 =====
def _tg_url(token: str, method: str) -> str:
    return f"{TELEGRAM_API.format(token=token)}/{method}"

async def tg_request(token: str, method: str, payload: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_tg_url(token, method), json=payload)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"❌ TG {method} 失败: {resp.status_code} {resp.text}")
                return None
    except Exception as e:
        logger.error(f"❌ TG {method} 异常: {e}")
        return None

async def send_message(token: str, chat_id: str, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_request(token, "sendMessage", payload)

async def edit_message(token: str, chat_id: str, message_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_request(token, "editMessageText", payload)

async def answer_callback(token: str, callback_id: str):
    return await tg_request(token, "answerCallbackQuery", {"callback_query_id": callback_id})

async def set_my_commands(token: str):
    """注册命令菜单（解决菜单问题）"""
    commands = [
        {"command": "start", "description": "🚀 启动雷达"},
        {"command": "menu", "description": "📋 打开控制台"},
        {"command": "scan", "description": "🔍 立即扫描一次"},
        {"command": "status", "description": "📊 查看运行状态"},
        {"command": "bind", "description": "🔗 绑定钱包地址"},
        {"command": "settings", "description": "⚙️ 设置过滤条件"},
        {"command": "help", "description": "❓ 帮助信息"},
    ]
    return await tg_request(token, "setMyCommands", {"commands": commands})

# ===== 键盘构建 =====
def main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔍 立即扫描", "callback_data": "scan"}],
            [
                {"text": "📊 状态", "callback_data": "status"},
                {"text": "⚙️ 设置", "callback_data": "settings"}
            ],
            [
                {"text": "🔗 绑定钱包", "callback_data": "bind"},
                {"text": "❓ 帮助", "callback_data": "help"}
            ]
        ]
    }

def scan_result_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔍 再次扫描", "callback_data": "scan"}],
            [{"text": "📋 返回菜单", "callback_data": "menu"}]
        ]
    }

# ===== 扫描逻辑 =====
async def run_scan_and_push():
    global scan_count, last_scan_result, last_snapshot
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    scan_count += 1
    logger.info(f"🔄 === 第 {scan_count} 次扫描 ===")

    raw = await fetch_airdrops()
    if not raw:
        last_scan_result = f"第{scan_count}次: 0 个项目"
        return 0

    # 评分 + 过滤
    scored = []
    for item in raw:
        if item["name"] not in WATCH_KEYWORDS and not item.get("is_claimable"):
            continue
        sc = score_project(
            item["name"],
            item.get("funding_amount", ""),
            item.get("is_claimable", False),
            item.get("premium", False),
            item.get("blockchains", [])
        )
        scored.append({**item, "score": sc})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # 去重推送
    pushed = 0
    for item in scored[:5]:
        key = item["name"]
        prev = last_snapshot.get(key, {})
        changed = prev.get("is_claimable") != item["is_claimable"] or prev.get("score", 0) != item["score"]
        if (changed and item["score"] >= 70) or (item["is_claimable"] and item["score"] >= 60):
            name_esc = item["name"].replace("*", "\\*").replace("_", "\\_")
            emoji = "🚨" if item["score"] >= 80 else "⚠️"
            msg = f"{emoji} *[{item['score']}分] {name_esc}*\n"
            msg += f"链: {', '.join(item.get('blockchains', [])) or 'N/A'}\n"
            msg += f"融资: {item.get('funding_amount') or 'N/A'}\n"
            msg += f"状态: {'✅ 可领取' if item.get('is_claimable') else '活跃'}"
            ok = await send_message(token, chat_id, msg)
            if ok:
                pushed += 1
            await asyncio.sleep(1)

    # 更新快照
    last_snapshot = {i["name"]: {"is_claimable": i.get("is_claimable", False), "score": i["score"]} for i in scored}
    last_scan_result = f"第{scan_count}次: 扫描{len(raw)}个, 推送{pushed}条"
    logger.info(f"✅ {last_scan_result}")
    return len(raw)

# ===== 处理更新 =====
async def handle_update(body: dict):
    global user_wallets, user_settings
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        return

    # 回调按钮
    cb = body.get("callback_query")
    if cb:
        chat_id = str(cb["message"]["chat"]["id"])
        msg_id = cb["message"]["message_id"]
        data = cb["data"]
        await answer_callback(token, cb["id"])

        if data == "menu":
            await edit_message(token, chat_id, msg_id,
                "📋 *空投雷达控制台*\n\n点击下方按钮操作：", main_keyboard())

        elif data == "scan":
            await edit_message(token, chat_id, msg_id, "⏳ *正在扫描全链空投，请稍候...*")
            total = await run_scan_and_push()
            panel = (
                f"✅ *扫描完成 | 第 {scan_count} 次*\n\n"
                f"🔍 扫描项目: `{total}` 个\n"
                f"📡 最近结果: `{last_scan_result}`\n"
                f"⏰ 时间: `{datetime.now().strftime('%H:%M:%S')}`\n\n"
                f"━━━━━━━━━━━━━\n🔴 系统运行中..."
            )
            await edit_message(token, chat_id, msg_id, panel, scan_result_keyboard())

        elif data == "status":
            status = (
                f"📊 *系统状态*\n\n"
                f"扫描次数: `{scan_count}`\n"
                f"最近结果: `{last_scan_result}`\n"
                f"绑定钱包: `{user_wallets.get(chat_id, '未绑定')}`\n"
                f"运行状态: ✅ 正常"
            )
            await edit_message(token, chat_id, msg_id, status, main_keyboard())

        elif data == "bind":
            await edit_message(token, chat_id, msg_id,
                "🔗 *绑定钱包*\n\n请直接发送你的 EVM 地址（0x 开头）：", None)

        elif data == "settings":
            await edit_message(token, chat_id, msg_id,
                "⚙️ *过滤设置*\n\n"
                "• 最低分数: 70 分\n"
                "• 监控链: 全部\n\n"
                "（完整设置功能开发中）", main_keyboard())

        elif data == "help":
            await edit_message(token, chat_id, msg_id,
                "📖 *帮助*\n\n"
                "`/start` 启动\n`/menu` 打开控制台\n`/scan` 立即扫描\n"
                "`/status` 查看状态\n`/bind` 绑定钱包\n`/settings` 过滤设置",
                main_keyboard())
        return

    # 文本消息
    msg = body.get("message")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    # 处理钱包绑定（用户直接发送地址）
    if text.startswith("0x") and len(text) > 10:
        user_wallets[chat_id] = text
        await send_message(token, chat_id, f"✅ 钱包绑定成功：\n`{text}`\n\n发送 /menu 打开控制台", main_keyboard())
        return

    if text == "/start" or text == "/menu":
        await send_message(token, chat_id,
            "👋 *空投雷达已激活！*\n\n发送 /menu 打开控制台，或点击输入框左侧的菜单按钮。",
            main_keyboard())

    elif text == "/scan":
        await send_message(token, chat_id, "⏳ 正在扫描...")
        total = await run_scan_and_push()
        await send_message(token, chat_id, f"✅ 扫描完成！共检查 {total} 个项目。\n\n{last_scan_result}")

    elif text == "/status":
        await send_message(token, chat_id,
            f"📊 *状态*\n扫描次数: {scan_count}\n最近: {last_scan_result}")

    elif text == "/bind":
        await send_message(token, chat_id, "🔗 请发送你的 EVM 地址（0x 开头）：")

    elif text == "/help":
        await send_message(token, chat_id,
            "📖 *帮助*\n/start 启动\n/menu 控制台\n/scan 扫描\n/status 状态\n/bind 绑钱包")

# ===== 生命周期 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 ===== Airdrop Radar 启动 =====")
    token = os.environ.get("TG_BOT_TOKEN")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    chat_id = os.environ.get("TG_CHAT_ID")

    # 1. 注册命令菜单（关键！让输入框左侧出现菜单按钮）
    if token:
        ok = await set_my_commands(token)
        if ok:
            logger.info("✅ 命令菜单注册成功")
        # 2. 设置 Webhook
        if render_url:
            webhook_url = f"{render_url}/webhook"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{TELEGRAM_API.format(token=token)}/setWebhook",
                    json={"url": webhook_url}
                )
                if resp.status_code == 200:
                    logger.info(f"✅ Webhook 设置成功: {webhook_url}")
                else:
                    logger.error(f"❌ Webhook 失败: {resp.text}")
        # 3. 上线通知
        if chat_id:
            await send_message(token, chat_id,
                f"🟢 *空投雷达已上线！*\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "每 15 分钟自动扫描一次。\n\n发送 /menu 打开控制台。")

    # 4. 定时扫描
    async def periodic():
        while True:
            try:
                await run_scan_and_push()
            except Exception as e:
                logger.error(f"❌ 定时扫描异常: {e}")
            await asyncio.sleep(900)
    task = asyncio.create_task(periodic())
    logger.info("⏰ 定时扫描已启动 (15分钟)")
    yield
    task.cancel()
    logger.info("🛑 服务关闭")

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    asyncio.create_task(handle_update(body))
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "alive", "scans": scan_count, "last": last_scan_result}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
