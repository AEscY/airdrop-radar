# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
from fastapi import FastAPI, Request
import httpx
from datetime import datetime

app = FastAPI()

# ========== 配置 ==========
ALPHA_DROPS_FREE = "https://alphadrops.net/free-crypto-airdrops"
ALPHA_DROPS_BEST = "https://alphadrops.net/best-crypto-airdrops-2026"
DROPS_API = "https://api.drops.bot/shared/v1/airdrops/{network}/{address}"
TELEGRAM_SEND = "https://api.telegram.org/bot{token}/sendMessage"

# 重点监控的项目（基于 Alpha Drops 2026 年 8 月真实榜单）
# 参考: alphadrops.net/free-crypto-airdrops 和 /best-crypto-airdrops-2026
WATCH_PROJECTS = [
    # 可领取空投（来自 free-crypto-airdrops 页）
    "Infinex", "Based", "RateX", "Pharos Network", "ETHGAS", "Lighter",
    "Superform", "Rainbow",
    # 最佳空投榜（来自 best-crypto-airdrops-2026 页）
    "Quote", "Arcus", "Robinhood Perpl", "Ondo Perps", "xStocks", "Fomo",
    "JTX", "Tori Finance", "Bulk", "Polymarket", "Pacifica", "QFEX",
    "N1", "Upshift",
    # 其他高潜力
    "Cambria", "Backpack", "Monad", "Berachain", "Perpl", "Orbinum",
    "Canopy", "Hypertrade", "Velvet", "Plume", "Grass", "MetaMask",
    "Pascal", "Titan", "Phoenix", "Extended", "Hylo", "RateX",
    "Arcus", "Nado", "Synq", "StandX", "Liquid", "Meridian"
]

# ========== 评分引擎 ==========
def score_drop(name, funding_str, chains, is_claimable, is_premium):
    s = 0
    # 融资额解析: "$44M" -> 44, "$2.88B" -> 2880
    m = re.search(r"\$?([\d.]+)\s*([MB])?", funding_str or "")
    if m:
        amt = float(m.group(1))
        if m.group(2) == "B":
            amt *= 1000
        s += min(amt / 10, 30)  # 融资权重封顶 30
    if is_claimable:
        s += 25
    if "Solana" in (chains or []):
        s += 5
    if is_premium:
        s += 10
    if is_claimable and funding_str:
        s += 5  # 可领取且有融资 = 高确定性
    # 知名 VC 背书项目加权
    big_vc_projects = ["Polymarket", "Fomo", "JTX", "Phoenix", "xStocks", "Ondo Perps", "Quote"]
    if name in big_vc_projects:
        s += 8
    return int(s)

# ========== 爬取 Alpha Drops 免费列表 ==========
async def fetch_alpha_drops():
    """
    爬取 alphadrops.net 免费列表页
    返回: [{"name", "funding_amount", "blockchains", "is_claimable", "premium", "score"}]
    """
    drops = []
    html_pages = []
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # 尝试多个页面
        for url in [ALPHA_DROPS_FREE, ALPHA_DROPS_BEST]:
            try:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                    }
                )
                if resp.status_code == 200 and len(resp.text) > 3000:
                    html_pages.append(resp.text)
            except Exception:
                continue
    
    if not html_pages:
        return []
    
    combined_html = "\n".join(html_pages)
    
    # 从 HTML 中提取项目信息
    for name in WATCH_PROJECTS:
        # 在合并的 HTML 中查找项目名
        idx = combined_html.find(name)
        if idx == -1:
            continue
        
        # 取项目名周围 1200 字符的上下文
        ctx_start = max(0, idx - 300)
        ctx_end = min(len(combined_html), idx + 900)
        ctx = combined_html[ctx_start:ctx_end]
        
        # 融资额
        funding_match = re.search(r"\$([\d.]+)\s*([MB])", ctx)
        funding = ""
        if funding_match:
            funding = f"${funding_match.group(1)}{funding_match.group(2)}"
        
        # 链识别
        chains = []
        chain_keywords = [
            "Solana", "Ethereum", "Base", "Arbitrum", "Polygon",
            "Abstract", "Monad", "BNB", "Hyperliquid", "Ink", "Sui",
            "SVM", "EVM", "L1", "L2"
        ]
        for chain in chain_keywords:
            if chain.lower() in ctx.lower():
                chains.append(chain)
        # 去重链
        chains = list(dict.fromkeys(chains))
        
        # 是否可领取
        is_claimable = "claimable" in ctx.lower()
        
        # 是否 Premium
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
    
    # 去重（同一项目可能出现多次）
    seen = {}
    for d in drops:
        if d["name"] not in seen:
            seen[d["name"]] = d
    return list(seen.values())

# ========== Drops API 钱包检查（可选）==========
async def check_wallet_drops(address, api_key):
    """
    调用 Drops 免费端点检查钱包可领取空投
    返回: [{"name", "usdValue", "addressUrl", "isExpiringSoon"}]
    """
    if not address or not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                DROPS_API.format(network="evm", address=address),
                headers={"x-api-key": api_key}
            )
            if resp.status_code == 200:
                data = resp.json()
                result = []
                for item in data.get("data", []):
                    result.append({
                        "name": item.get("airdropName", "Unknown"),
                        "usdValue": item.get("usdValue", 0),
                        "addressUrl": item.get("addressUrl", ""),
                        "isExpiringSoon": item.get("isExpiringSoon", False)
                    })
                return result
    except Exception:
        pass
    return []

# ========== Telegram 推送 ==========
async def notify(token, chat_id, text):
    """推送消息到 Telegram，自动处理 UTF-8 编码"""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            TELEGRAM_SEND.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            },
            headers={"Content-Type": "application/json; charset=utf-8"}
        )

# ========== Cloudflare KV 读写 ==========
async def kv_get(env, key):
    """
    从 Cloudflare KV 读取值
    需要环境变量: KV_ACCOUNT_ID, KV_NAMESPACE_ID, CF_API_TOKEN
    """
    account_id = env.get("KV_ACCOUNT_ID")
    namespace_id = env.get("KV_NAMESPACE_ID")
    api_token = env.get("CF_API_TOKEN")
    
    if not all([account_id, namespace_id, api_token]):
        return None
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}",
                headers={"Authorization": f"Bearer {api_token}"}
            )
            if r.status_code == 200:
                return r.text
    except Exception:
        pass
    return None

async def kv_put(env, key, value):
    """
    写入 Cloudflare KV
    需要环境变量: KV_ACCOUNT_ID, KV_NAMESPACE_ID, CF_API_TOKEN
    """
    account_id = env.get("KV_ACCOUNT_ID")
    namespace_id = env.get("KV_NAMESPACE_ID")
    api_token = env.get("CF_API_TOKEN")
    
    if not all([account_id, namespace_id, api_token]):
        return
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.put(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}",
                content=value.encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/octet-stream"
                }
            )
    except Exception:
        pass

# ========== 主雷达逻辑 ==========
async def run_radar():
    env = dict(os.environ)
    token = env.get("TG_BOT_TOKEN")
    chat_id = env.get("TG_CHAT_ID")
    wallet = env.get("WATCH_WALLET")
    drops_api_key = env.get("DROPS_API_KEY")
    
    # 1. 爬取 Alpha Drops
    drops = await fetch_alpha_drops()
    
    # 2. 读上次快照
    prev_raw = await kv_get(env, "last_snapshot")
    prev = json.loads(prev_raw) if prev_raw else {}
    
    # 3. diff + 过滤
    new_signals = []
    for d in drops:
        key = d["name"]
        prev_item = prev.get(key, {})
        changed = (prev_item.get("is_claimable") != d["is_claimable"] or
                   prev_item.get("score", 0) != d["score"])
        # 新出现的高分 OR 状态变化的
        if (changed and d["score"] >= 70) or (d["is_claimable"] and d["score"] >= 60):
            new_signals.append(d)
    
    # 4. 高分优先，最多推 5 条
    new_signals.sort(key=lambda x: x["score"], reverse=True)
    pushed = 0
    for d in new_signals[:5]:
        emoji = "🚨" if d["score"] >= 80 else "⚠️"
        msg = f"{emoji} [{d['score']}分] {d['name']}\n"
        msg += f"链: {', '.join(d['blockchains']) or 'N/A'}\n"
        msg += f"融资: {d['funding_amount'] or 'N/A'}\n"
        msg += f"状态: {'✅ 可领取' if d['is_claimable'] else '活跃'}\n"
        if d["premium"]:
            msg += f"级别: Premium\n"
        msg += f"来源: alphadrops.net"
        await notify(token, chat_id, msg)
        pushed += 1
        await asyncio.sleep(0.5)  # 避免 Telegram 限速
    
    # 5. 钱包空投检查（可选）
    if wallet and drops_api_key:
        wallet_drops = await check_wallet_drops(wallet, drops_api_key)
        for wd in wallet_drops:
            if wd["usdValue"] > 0 or wd["isExpiringSoon"]:
                emoji = "💰" if wd["usdValue"] > 0 else "⏰"
                msg = f"{emoji} 钱包空投: {wd['name']}\n"
                msg += f"估值: ${wd['usdValue']}\n"
                if wd["isExpiringSoon"]:
                    msg += f"⚠️ 即将过期!\n"
                msg += f"详情: {wd['addressUrl']}"
                await notify(token, chat_id, msg)
                await asyncio.sleep(0.5)
    
    # 6. 存快照
    snapshot = {d["name"]: {
        "is_claimable": d["is_claimable"],
        "score": d["score"]
    } for d in drops}
    await kv_put(env, "last_snapshot", json.dumps(snapshot, ensure_ascii=False))
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"OK: 扫描 {len(drops)} 个空投，推送 {pushed} 条新信号，时间 {now}"

# ========== 端点 ==========
@app.get("/health")
async def health():
    return {"status": "alive", "ts": datetime.now().isoformat()}

@app.post("/collect")
async def collect(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {os.environ.get('RENDER_SECRET')}":
        return {"error": "unauthorized"}, 401
    result = await run_radar()
    return {"result": result}

@app.get("/run")
async def manual_run():
    """手动触发，用于测试"""
    result = await run_radar()
    return {"result": result}

@app.get("/")
async def root():
    return {
        "service": "Airdrop Radar",
        "status": "running",
        "endpoints": ["/health", "/run", "/collect"]
    }

# ========== 本地运行入口 ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
